# Synthetic avatar asset provenance

The six built-in identities under `symposium/viewer/static/avatars/` were
generated specifically for Symposium on 2026-08-11 with the built-in OpenAI
image-generation tool. The additional `pool-001.webp` through
`pool-050.webp` identities were generated on 2026-08-13 with the same tool and
visual specification. They depict fictional people and are not intended to
represent, imitate, or imply endorsement by any real person.

Every image was reviewed, resized to 768 × 768, and encoded as WebP quality 86
for the packaged viewer. The viewer labels the collection as synthetic and
each image has synthetic-portrait alternative text. These are canonical stills
for the zero-cost viewer and never need to be uploaded to a rendering provider.

## Shared generation prompt

```text
Use case: photorealistic-natural
Asset type: canonical video-call avatar portrait for an AI agent in Symposium
Scene/backdrop: dark neutral charcoal studio backdrop with a subtle cool
gradient, no objects
Style/medium: high-end natural portrait photography, not CGI or illustration
Composition/framing: square, centered head and shoulders, eye-level webcam
framing, looking directly into camera, full face and shoulders visible
Lighting/mood: soft realistic key light, restrained fill, warm and trustworthy,
compatible with a dark video-call interface
Constraints: completely invented identity; must not resemble a known or public
person; one adult; realistic skin and hair; contemporary professional clothing;
no text, logo, watermark, border, hands, dramatic pose, or cropped forehead
Avoid: stylization, plastic skin, beauty filters, uncanny symmetry, exaggerated
expressions, cinematic props
```

The subject variation for each asset was:

| File | Subject direction |
|---|---|
| `logician.webp` | calm analytical professional, neutral attentive expression |
| `visionary.webp` | South Asian woman, late thirties, imaginative and grounded |
| `researcher.webp` | East Asian woman, early fifties, observant and composed |
| `critic.webp` | Black man, mid forties, rigorous and constructive |
| `engineer.webp` | Latin American woman, early thirties, pragmatic and capable |
| `coordinator.webp` | Mediterranean man, late fifties, discreet and attentive |

## New-agent pool assignment

The control plane assigns every newly created agent one random, unused identity
from the 50-image pool and persists the selected `avatar_id`. An identity is
never silently reused, including when its agent is outside the active room.
Before creation, Sartori shows the candidate portrait and the user can request
another unused one. A legacy or unknown run persona without a registered image
still receives a deterministic initials card rendered by local HTML/CSS.

The pool is balanced between 25 feminine and 25 masculine presentations. Each
portrait carries an explicit Italian voice profile: feminine identities use
the model's Julia speaker and masculine identities use Richard. Voice choice
is based on this registered avatar metadata, never inferred from an agent name.
Age/tone/rhythm descriptions provide consistent variation while preserving
the same base Italian speaker for a given visual presentation.

If more photorealistic identities are desired, generate them only with a local
or already-included image tool, review that each depicts a fictional person,
convert them to the same square WebP format, and register them in the catalog.
The runtime must never require a paid image-generation or avatar-rendering service. Never
use a real person's likeness without documented authorization.

## Pool generation directions

Each of the 50 images was produced with a separate generation call, rather
than cropped from a contact sheet. Subject directions alternate feminine and
masculine presentation and vary apparent adult age, visual ancestry, hair,
clothing color, and professional demeanor. All use the shared prompt above.
The delivered files are 768 × 768 WebP quality 86; the complete portrait
directory is roughly 2 MB and all 50 pool files have distinct SHA-256 hashes.
