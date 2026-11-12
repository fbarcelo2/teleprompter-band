#!/usr/bin/env bash
# Instala el lanzador de escritorio apuntando a esta copia del proyecto.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${XDG_DATA_HOME:-$HOME/.local/share}/applications/teleprompter.desktop"
PYTHON="$(command -v python3)"

if [ ! -f "$HERE/guion.txt" ]; then
    echo "Falta guion.txt junto a teleprompter.py" >&2
    exit 1
fi

mkdir -p "$(dirname "$TARGET")"
sed -e "s|@DIR@|$HERE|g" -e "s|@PYTHON@|$PYTHON|g" \
    "$HERE/teleprompter.desktop.in" > "$TARGET"
chmod +x "$HERE/teleprompter.py"

echo "Lanzador instalado en: $TARGET"
echo
echo "Para tenerlo en la barra:"
echo "  clic derecho en el panel > Panel > Agregar elementos nuevos > Lanzador"
echo "  y elegí Teleprompter. O arrastralo desde el menú de aplicaciones."
echo
echo "Sin panel, también sirve:"
echo "  $PYTHON $HERE/teleprompter.py --config $HERE/config.json --toggle"
