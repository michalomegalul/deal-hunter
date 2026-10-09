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
FROM public.ecr.aws/docker/library/python:3.13-slim
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=build /app/.venv /app/.venv
RUN useradd --create-home hunter
USER hunter
WORKDIR /home/hunter
ENTRYPOINT ["hunt"]
CMD ["--help"]
