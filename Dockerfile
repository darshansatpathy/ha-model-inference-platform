# One image for the model server, the gateway and the rollout controller.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MODEL_DIR=/models PORT=8080
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin ifp
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
COPY --chown=root:root models /models
USER 10001
EXPOSE 8080
ENTRYPOINT ["ifp"]
CMD ["serve"]
