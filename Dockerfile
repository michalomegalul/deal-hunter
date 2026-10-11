# Deal Hunter image: the `hunt` command and its dependencies.

# --- build the virtualenv ---
# Base images from the ECR mirror of Docker Hub: shared CI runners hit Docker Hub's pull rate limit.
FROM public.ecr.aws/docker/library/python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# --- the runtime image (no uv, no build cache) ---
FROM public.ecr.aws/docker/library/python:3.13-slim AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=build /app/.venv /app/.venv
RUN useradd --create-home hunter
USER hunter
WORKDIR /home/hunter
# The worker's LLM answer cache. A named volume mounted here starts out owned by `hunter`.
RUN mkdir -p /home/hunter/data/llm_cache
ENTRYPOINT ["hunt"]
CMD ["--help"]

# --- fb variant: the only source that needs a real browser (see facebook.py) ---
# A separate build target (and a separate, much bigger published image, :<tag>-fb) so the plain
# image above stays small for everyone who isn't running the fb-scraper compose service.
FROM public.ecr.aws/docker/library/python:3.13-slim AS fb-build
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project --extra fb
COPY src ./src
RUN uv sync --locked --no-dev --no-editable --extra fb

FROM public.ecr.aws/docker/library/python:3.13-slim AS fb
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=fb-build /app/.venv /app/.venv
# System deps as root (apt); the browser binary itself goes to hunter's own cache dir below.
RUN python -m playwright install-deps chromium
RUN useradd --create-home hunter
USER hunter
WORKDIR /home/hunter
RUN python -m playwright install chromium
ENTRYPOINT ["hunt"]
CMD ["scrape-fb", "--loop"]
