#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${QDRANT_CONTAINER_NAME:-qdrant-sokratic}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed or not in PATH."
  exit 1
fi

if docker ps --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  echo "Stopping ${CONTAINER_NAME}..."
  docker stop "${CONTAINER_NAME}" >/dev/null
  echo "Qdrant stopped."
elif docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  echo "Container ${CONTAINER_NAME} is already stopped."
else
  echo "No container named ${CONTAINER_NAME} found."
fi
