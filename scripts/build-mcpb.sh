#!/usr/bin/env bash
# Собирает dist/mcp-devian-<версия>.mcpb для установки двойным кликом в Claude Desktop.
# Нужны uv и node (npx). Claude Desktop сам поставит Python и зависимости по uv.lock.
set -euo pipefail

cd "$(dirname "$0")/.."
MCPB="npx -y @anthropic-ai/mcpb@2.1.2"
VERSION=$(uv run --no-project python -c 'import json; print(json.load(open("manifest.json"))["version"])')
PYPROJECT_VERSION=$(uv version --short)
if [ "$VERSION" != "$PYPROJECT_VERSION" ]; then
  echo "Версии не совпадают: manifest.json $VERSION, pyproject.toml $PYPROJECT_VERSION" >&2
  exit 1
fi

# В пакет попадает только то, что лежит в git: новый модуль без git add пропал бы молча.
UNTRACKED=$(git ls-files --others --exclude-standard src manifest.json pyproject.toml README.md)
if [ -n "$UNTRACKED" ]; then
  echo "Не добавлены в git (git add):" >&2
  echo "$UNTRACKED" >&2
  exit 1
fi
if ! git diff --quiet -- src manifest.json pyproject.toml README.md uv.lock; then
  echo "Есть изменения не в индексе: сделай git add, иначе в пакет уйдёт старая версия файлов." >&2
  exit 1
fi

uv lock --check
STAGE=build/stage
rm -rf "$STAGE" && mkdir -p "$STAGE" dist
# Берём файлы из индекса git: ничего лишнего и никаких локальных секретов.
git ls-files -z | grep -zv -e '^tests/' -e '^scripts/' -e '^\.github/' | xargs -0 -I{} rsync -R "{}" "$STAGE/"
cp uv.lock "$STAGE/"

$MCPB validate "$STAGE/manifest.json"
$MCPB pack "$STAGE" "dist/mcp-devian-$VERSION.mcpb"
echo "Готово: dist/mcp-devian-$VERSION.mcpb"
