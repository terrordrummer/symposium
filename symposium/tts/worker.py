"""Long-lived JSON-lines worker for local Parler-TTS synthesis."""

from __future__ import annotations

import json
import os
import sys
import wave
from pathlib import Path

from symposium.tts.local import MODEL_ID, MODEL_REVISION


class Synthesizer:
    def __init__(self) -> None:
        import torch
        from parler_tts import ParlerTTSForConditionalGeneration
        from transformers import AutoTokenizer

        # Parler's audio codec is the reliable part of this stack on CPU. The
        # M4 Max still provides practical latency and avoids MPS-only operator
        # failures that would otherwise surface halfway through an utterance.
        self.torch = torch
        self.device = "cpu"
        self.model = ParlerTTSForConditionalGeneration.from_pretrained(
            MODEL_ID,
            revision=MODEL_REVISION,
        ).to(self.device)
        self.description_tokenizer = AutoTokenizer.from_pretrained(
            self.model.config.text_encoder._name_or_path,
        )
        self.prompt_tokenizer = AutoTokenizer.from_pretrained(
            MODEL_ID,
            revision=MODEL_REVISION,
        )
        self.sample_rate = int(self.model.config.sampling_rate)

    def synthesize(self, text: str, description: str, output: Path) -> None:
        description_ids = self.description_tokenizer(
            description,
            return_tensors="pt",
        ).input_ids.to(self.device)
        prompt_ids = self.prompt_tokenizer(
            text,
            return_tensors="pt",
        ).input_ids.to(self.device)
        with self.torch.inference_mode():
            generation = self.model.generate(
                input_ids=description_ids,
                prompt_input_ids=prompt_ids,
            )
        samples = generation.cpu().float().numpy().squeeze()
        samples = samples.clip(-1.0, 1.0)
        pcm = (samples * 32767.0).astype("<i2").tobytes()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".tmp.wav")
        with wave.open(str(temporary), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(self.sample_rate)
            handle.writeframes(pcm)
        os.replace(temporary, output)


def main() -> int:
    synthesizer = None
    for raw in sys.stdin:
        request = {}
        try:
            request = json.loads(raw)
            if request.get("action") == "shutdown":
                return 0
            if synthesizer is None:
                synthesizer = Synthesizer()
            synthesizer.synthesize(
                str(request["text"]),
                str(request["description"]),
                Path(request["output"]),
            )
            response = {"id": request.get("id"), "ok": True}
        except Exception as exc:  # noqa: BLE001 — worker returns structured failures
            response = {
                "id": request.get("id") if isinstance(request, dict) else None,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
