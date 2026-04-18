#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "Project root: ${ROOT_DIR}"
echo "Using Python: ${PYTHON_BIN}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Error: ${PYTHON_BIN} not found in PATH."
  exit 1
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Creating virtual environment at ${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
else
  echo "Virtual environment already exists at ${VENV_DIR}"
fi

echo "Activating virtual environment"
source "${VENV_DIR}/bin/activate"

echo "Upgrading pip/setuptools/wheel"
python -m pip install --upgrade pip setuptools wheel

echo "Installing pinned dependencies from requirements.txt"
python -m pip install -r "${ROOT_DIR}/requirements.txt"

echo
echo "Setup complete."
echo "Activate later with: source .venv/bin/activate"
