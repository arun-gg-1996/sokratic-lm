#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${QDRANT_CONTAINER_NAME:-qdrant-sokratic}"
IMAGE="${QDRANT_IMAGE:-qdrant/qdrant:v1.13.4}"
HOST_PORT_HTTP="${QDRANT_HTTP_PORT:-6333}"
HOST_PORT_GRPC="${QDRANT_GRPC_PORT:-6334}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${QDRANT_DATA_DIR:-${ROOT_DIR}/.qdrant_data}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed or not in PATH."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Error: docker daemon is not running."
  exit 1
fi

mkdir -p "${DATA_DIR}"

if docker ps --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  echo "Qdrant is already running in container ${CONTAINER_NAME}."
  echo "HTTP endpoint: http://localhost:${HOST_PORT_HTTP}"
  exit 0
fi

if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  echo "Starting existing container ${CONTAINER_NAME}..."
  docker start "${CONTAINER_NAME}" >/dev/null
else
  echo "Creating and starting ${CONTAINER_NAME} with image ${IMAGE}..."
  docker run -d \
    --name "${CONTAINER_NAME}" \
    -p "${HOST_PORT_HTTP}:6333" \
    -p "${HOST_PORT_GRPC}:6334" \
    -v "${DATA_DIR}:/qdrant/storage" \
    "${IMAGE}" >/dev/null
fi

echo "Qdrant is up."
echo "HTTP endpoint: http://localhost:${HOST_PORT_HTTP}"
