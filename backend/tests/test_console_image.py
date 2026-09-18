"""The console image serves a static bundle, unprivileged.

It ran `npm run dev --host 0.0.0.0` as root — Vite's development server,
serving transformed source and a live-reload socket, which docker compose
published on every host interface. Measured against that image: uid 0,
`/@vite/client` answered with the development client, `/src/main.tsx` with the
app's source.

The CI image job asserts the same on a *running* container; this is the fast
half, so a Dockerfile change fails on every push rather than only where Docker
is available.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "frontend" / "Dockerfile"

# Images whose default user is not root. A final stage on anything else must
# say `USER` itself.
UNPRIVILEGED_BASES = ("nginxinc/nginx-unprivileged:", "gcr.io/distroless/")


def final_stage() -> list[str]:
    instructions = [
        line.strip()
        for line in DOCKERFILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    starts = [i for i, line in enumerate(instructions) if line.upper().startswith("FROM ")]
    assert starts, "frontend/Dockerfile has no FROM at all"
    return instructions[starts[-1] :]


def _as_words(stage: list[str]) -> str:
    """The stage with exec-form punctuation removed.

    `CMD ["npm", "run", "dev"]` is how a Dockerfile usually writes it, and a
    literal search for "npm run dev" never matches that form — the first
    version of this test passed with exactly that line added.
    """
    text = " ".join(stage).lower()
    for character in '[]",':
        text = text.replace(character, " ")
    return " ".join(text.split())


def test_the_console_image_does_not_run_a_development_server():
    stage = _as_words(final_stage())
    for marker in ("npm run dev", "vite", "npm start", "node_modules/.bin"):
        assert marker not in stage, (
            f"the console's final image stage mentions {marker!r}; it must serve the "
            f"built bundle, not run a development server"
        )


def test_the_console_image_does_not_run_as_root():
    stage = final_stage()
    base = stage[0].split()[1]
    users = [line.split()[1] for line in stage if line.upper().startswith("USER ")]
    if users:
        assert users[-1].split(":")[0] not in ("root", "0"), "the console image runs as root"
        return
    assert base.startswith(UNPRIVILEGED_BASES), (
        f"the console image's final stage is {base!r}, which runs as root by default, "
        f"and sets no USER"
    )


def test_the_final_stage_ships_the_built_bundle():
    """Vacuity guard: a final stage that copied nothing passes both tests above."""
    stage = " ".join(final_stage())
    assert "--from=build" in stage and "/app/dist" in stage, (
        "the console image's final stage does not copy the built bundle"
    )
