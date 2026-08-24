"""Lifecycle and cache management for the isolated local Parler-TTS worker.

The core Symposium environment deliberately has no machine-learning runtime
dependency. The browser can ask this manager to install a pinned worker into
the private workspace, after which synthesis is local and requires no API key.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import venv
from pathlib import Path
from typing import Any, Optional, TextIO

MODEL_ID = "parler-tts/parler-tts-mini-multilingual-v1.1"
MODEL_REVISION = "11b27d57855dec1ce0914ba1f12363bf2ea75ba3"
PARLER_REVISION = "d108732cd57788ec86bc857d99a6cabd66663d68"
AUDIO_TOOLS_REVISION = "348ebf2034ce24e2a91a553e3171cb00c0c71678"


class LocalTTSUnavailable(RuntimeError):
    """Raised when local speech is not ready or cannot synthesize a clip."""


class LocalTTSManager:
    """Install, run and cache one private local TTS runtime."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.runtime_dir = self.state_dir / "tts-runtime"
        self.cache_dir = self.state_dir / "tts-cache"
        self.model_cache = self.state_dir / "huggingface"
        self.status_path = self.state_dir / "tts-status.json"
        self.ready_path = self.runtime_dir / ".ready.json"
        self._install_thread: Optional[threading.Thread] = None
        self._worker: Optional[subprocess.Popen[str]] = None
        self._worker_log: Optional[TextIO] = None
        self._worker_lock = threading.Lock()
        self._request_sequence = 0

    @property
    def python(self) -> Path:
        return self.runtime_dir / "bin" / "python"

    def public_status(self) -> dict[str, Any]:
        status = self._read_status()
        installing = self._install_thread is not None and self._install_thread.is_alive()
        if installing:
            state = "installing"
        elif self.ready_path.is_file() and self.python.is_file():
            state = "ready"
        elif status.get("state") == "error":
            state = "error"
        else:
            state = "setup_required"
        return {
            "engine": "Parler-TTS Mini Multilingual v1.1",
            "model": MODEL_ID,
            "state": state,
            "phase": status.get("phase"),
            "message": status.get("message"),
            "local_only": True,
            "api_key_required": False,
            "voice_presentations": ["feminine", "masculine"],
        }

    def install(self) -> dict[str, Any]:
        if self.ready_path.is_file() and self.python.is_file():
            return self.public_status()
        if self._install_thread is None or not self._install_thread.is_alive():
            self._install_thread = threading.Thread(
                target=self._install_runtime,
                name="symposium-tts-install",
                daemon=True,
            )
            self._install_thread.start()
        return self.public_status()

    def synthesize(self, text: str, voice_description: str) -> Path:
        clean_text = " ".join(str(text).split()).strip()
        if not clean_text:
            raise ValueError("speech text must not be empty")
        if len(clean_text) > 1_500:
            raise ValueError("speech text is too long")
        if not self.ready_path.is_file() or not self.python.is_file():
            raise LocalTTSUnavailable("la voce locale non è ancora pronta")

        cache_key = hashlib.sha256(json.dumps(
            {
                "model": MODEL_ID,
                "revision": MODEL_REVISION,
                "description": voice_description,
                "text": clean_text,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")).hexdigest()
        output = self.cache_dir / f"{cache_key}.wav"
        if output.is_file() and output.stat().st_size > 44:
            return output

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with self._worker_lock:
            if output.is_file() and output.stat().st_size > 44:
                return output
            worker = self._ensure_worker()
            self._request_sequence += 1
            request_id = f"tts-{self._request_sequence:08d}"
            request = {
                "id": request_id,
                "text": clean_text,
                "description": voice_description,
                "output": str(output),
            }
            try:
                assert worker.stdin is not None
                assert worker.stdout is not None
                worker.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                worker.stdin.flush()
                raw = worker.stdout.readline()
            except (BrokenPipeError, OSError) as exc:
                self._stop_worker()
                raise LocalTTSUnavailable(f"il motore vocale locale si è arrestato: {exc}") from exc
            if not raw:
                self._stop_worker()
                raise LocalTTSUnavailable(
                    "il motore vocale locale non ha restituito audio; "
                    f"consulta {self.state_dir / 'tts-worker.log'}"
                )
            try:
                response = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LocalTTSUnavailable("risposta non valida dal motore vocale") from exc
            if response.get("id") != request_id or not response.get("ok"):
                raise LocalTTSUnavailable(
                    str(response.get("error") or "sintesi vocale locale non riuscita")
                )
        if not output.is_file() or output.stat().st_size <= 44:
            raise LocalTTSUnavailable("il motore vocale non ha creato un file audio valido")
        return output

    def close(self) -> None:
        with self._worker_lock:
            self._stop_worker()

    def _ensure_worker(self) -> subprocess.Popen[str]:
        if self._worker is not None and self._worker.poll() is None:
            return self._worker
        environment = os.environ.copy()
        environment.update({
            "HF_HOME": str(self.model_cache),
            "TOKENIZERS_PARALLELISM": "false",
            "PYTHONUNBUFFERED": "1",
        })
        log_path = self.state_dir / "tts-worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._worker_log = open(log_path, "a", encoding="utf-8")
        self._worker = subprocess.Popen(
            [str(self.python), "-m", "symposium.tts.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._worker_log,
            text=True,
            bufsize=1,
            env=environment,
            cwd=Path(__file__).resolve().parents[2],
        )
        return self._worker

    def _stop_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None and worker.poll() is None:
            try:
                if worker.stdin is not None:
                    worker.stdin.write(json.dumps({"action": "shutdown"}) + "\n")
                    worker.stdin.flush()
                worker.wait(timeout=3)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                worker.terminate()
        if self._worker_log is not None:
            self._worker_log.close()
        self._worker_log = None

    def _install_runtime(self) -> None:
        try:
            self._write_status("installing", "runtime", "Creo l’ambiente vocale isolato…")
            if not self.python.is_file():
                self.runtime_dir.parent.mkdir(parents=True, exist_ok=True)
                venv.EnvBuilder(with_pip=True, system_site_packages=True).create(
                    self.runtime_dir
                )
            self._write_status(
                "installing", "dependencies", "Installo il motore vocale locale…"
            )
            pip = [str(self.python), "-m", "pip", "install", "--disable-pip-version-check"]
            self._run_install([
                *pip,
                "transformers==4.46.1",
                "sentencepiece>=0.2",
                "protobuf>=4",
                "descript-audio-codec",
                (
                    "descript-audiotools @ git+https://github.com/descriptinc/"
                    f"audiotools.git@{AUDIO_TOOLS_REVISION}"
                ),
            ])
            self._run_install([
                *pip,
                "--no-deps",
                (
                    "parler-tts @ git+https://github.com/huggingface/"
                    f"parler-tts.git@{PARLER_REVISION}"
                ),
            ])
            self._write_status(
                "installing",
                "model",
                "Scarico una volta il modello vocale (circa 3,8 GB)…",
            )
            environment = os.environ.copy()
            environment["HF_HOME"] = str(self.model_cache)
            subprocess.run(
                [
                    str(self.python),
                    "-c",
                    (
                        "from huggingface_hub import snapshot_download; "
                        f"snapshot_download({MODEL_ID!r}, revision={MODEL_REVISION!r})"
                    ),
                ],
                check=True,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.ready_path.write_text(json.dumps({
                "model": MODEL_ID,
                "revision": MODEL_REVISION,
                "parler_revision": PARLER_REVISION,
            }, indent=2) + "\n", encoding="utf-8")
            self._write_status("ready", "complete", "Voce locale pronta.")
        except Exception as exc:  # noqa: BLE001 — status must preserve installer failures
            self._write_status("error", "failed", f"Installazione non riuscita: {exc}")

    @staticmethod
    def _run_install(command: list[str]) -> None:
        subprocess.run(command, check=True, capture_output=True, text=True)

    def _write_status(self, state: str, phase: str, message: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "state": state,
            "phase": phase,
            "message": message,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.status_path)

    def _read_status(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.status_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
