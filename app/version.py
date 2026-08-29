"""Build identity: which image this process is, and which commit it came from.

Baked in at image build time, not read from `config.yaml`. The values arrive as
environment variables that the Dockerfile sets from `ARG`s, which the GitHub
Actions workflow fills in from the run that built the image:

    build-arg APP_VERSION=master-<run_number>   ->  ENV APP_VERSION
    build-arg APP_REVISION=<commit sha>         ->  ENV APP_REVISION

`APP_VERSION` is deliberately the **same string as the image tag**, so what the
process reports and what `docker ps` shows are directly comparable. That is the
point of this module: the deploy pins an explicit `master-N` tag in the compose
file, and until now nothing could confirm that the code actually answering
requests was that build. A compose file pinning `master-52` while a stale
container keeps running `master-50` looked identical from the outside.

Read once at import — this is build metadata, not configuration, so unlike
everything in `config.py` it is never hot-reloaded and never changes for the life
of the process.

Outside a built image (running from a checkout, or in tests) the variables are
unset and `VERSION` is `"dev"`, which is a truthful answer rather than a
misleading version number.
"""
import os


def _env(name: str):
    """An environment value, or None when unset or blank.

    Blank matters: `docker build` with an undefined `ARG` sets the variable to
    the empty string rather than leaving it out, so `""` has to mean "not
    provided" or every local build would report a version of `""`.
    """
    return (os.environ.get(name) or "").strip() or None


# The image tag this build was published under, e.g. "master-52". "dev" when
# running outside an image built by CI.
VERSION = _env("APP_VERSION") or "dev"

# Full git SHA the image was built from. None outside CI.
REVISION = _env("APP_REVISION")

# True when this is a real CI build rather than a working copy.
IS_RELEASE = VERSION != "dev"


def short_revision():
    """First 8 chars of the commit sha, or None — what you actually paste into
    a `git show`."""
    return REVISION[:8] if REVISION else None


def summary() -> str:
    """One-line human form: `master-52 (1e5837a3)`, or just `dev`."""
    rev = short_revision()
    return f"{VERSION} ({rev})" if rev else VERSION


def as_dict() -> dict:
    """Serializable form for `/health` and the admin API."""
    return {"version": VERSION, "revision": REVISION, "release": IS_RELEASE}
