#!/usr/bin/env python3
"""
csv_to_labplot.py

Convertit un fichier CSV "multi-graphiques" (format export type ACP10 / MiniC)
directement en un projet LabPlot (.lml), prêt à être ouvert dans LabPlot.

Structure attendue du fichier d'entrée :
- X lignes d'en-tête, chacune commençant par "% TYPE=..." et décrivant un
  graphique (TITLE, XUNIT, YUNIT, etc.), sous forme de paires clé=valeur.
- Toutes les lignes suivantes contiennent les données, avec 2*X colonnes :
  pour chaque graphique i, une colonne "abscisse (temps)" suivie d'une
  colonne "ordonnée (mesure)".

Le script ne fait aucune hypothèse figée sur le nombre de graphiques : X est
déduit automatiquement du nombre de lignes d'en-tête "% TYPE=...".

Sortie : un fichier .lml (XML LabPlot compressé en XZ) contenant :
- une feuille de calcul ("Feuille de calcul") avec une colonne de temps
  (dédupliquée si elle est identique pour tous les graphiques) suivie d'une
  colonne par grandeur mesurée,
- une feuille de tracé ("worksheet") par grandeur mesurée, contenant un
  graphique cartésien avec une courbe (mesure en fonction du temps),
  échelles en ajustement automatique.

Usage :
    python csv_to_labplot.py chemin/vers/fichier.csv [--output projet.lml]
"""

import argparse
import base64
import csv
import datetime
import re
import struct
import sys
import uuid
import lzma
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape, quoteattr as xml_attr


HEADER_PREFIX = "% TYPE="

# Regex générique pour extraire les paires clé=valeur d'une ligne d'en-tête.
KEYVAL_RE = re.compile(r'(\w+)\s*=\s*("[^"]*"|[^\s,]+)')


# --------------------------------------------------------------------------- #
# Parsing du CSV d'entrée
# --------------------------------------------------------------------------- #

def parse_header_line(line: str) -> dict:
    fields = {}
    for key, value in KEYVAL_RE.findall(line):
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        fields[key] = value
    return fields


def read_input_file(path: Path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        raw_lines = f.readlines()
    return [line.rstrip("\r\n") for line in raw_lines]


def load_graphs(input_path: Path):
    """Renvoie (graphs_meta, columns_values) où columns_values est une liste
    de listes de float, une par colonne de donnée (2*X colonnes), dans
    l'ordre d'origine du fichier."""
    lines = read_input_file(input_path)

    header_lines = []
    first_data_index = None
    for idx, line in enumerate(lines):
        if line.strip().startswith(HEADER_PREFIX):
            header_lines.append(line)
        else:
            if line.strip() != "":
                first_data_index = idx
                break

    if not header_lines:
        sys.exit("Erreur : aucune ligne d'en-tête '% TYPE=...' trouvée dans le fichier.")
    if first_data_index is None:
        sys.exit("Erreur : aucune ligne de données trouvée après les en-têtes.")

    nb_graphs = len(header_lines)
    print(f"Nombre de graphiques détectés (X) : {nb_graphs}")

    graphs_meta = [parse_header_line(h) for h in header_lines]

    data_lines = lines[first_data_index:]
    reader = csv.reader(data_lines)
    rows = [row for row in reader if any(cell.strip() != "" for cell in row)]
    if not rows:
        sys.exit("Erreur : aucune donnée exploitable trouvée.")

    nb_cols_attendues = 2 * nb_graphs
    if len(rows[0]) < nb_cols_attendues:
        sys.exit(
            f"Erreur : {nb_cols_attendues} colonnes attendues (2 x {nb_graphs} "
            f"graphiques) mais seulement {len(rows[0])} colonnes trouvées."
        )

    # Transposition + conversion en float
    columns_values = []
    for col_idx in range(nb_cols_attendues):
        col = []
        for row in rows:
            try:
                col.append(float(row[col_idx]))
            except ValueError:
                col.append(float("nan"))
        columns_values.append(col)

    return graphs_meta, columns_values


# --------------------------------------------------------------------------- #
# Construction des colonnes de la feuille de calcul (déduplication du temps)
# --------------------------------------------------------------------------- #

def build_spreadsheet_columns(graphs_meta, columns_values):
    """Construit la liste des colonnes finales : (nom, valeurs), en
    dédupliquant les colonnes strictement identiques (typiquement le temps)."""
    column_names = []
    for meta in graphs_meta:
        title = meta.get("TITLE", "")
        xunit = meta.get("XUNIT", "")
        yunit = meta.get("YUNIT", "")
        header_time = f"Temps [{xunit}]" if xunit else "Temps"
        header_value = f"{title} [{yunit}]" if yunit else (title or "Valeur")
        column_names.append(header_time)
        column_names.append(header_value)

    seen = []          # liste de tuples de valeurs déjà rencontrées
    seen_names = []     # nom de colonne associé à chaque tuple conservé
    kept = []            # (nom, valeurs) conservés dans l'ordre

    for name, values in zip(column_names, columns_values):
        key = tuple(values)
        if key in seen:
            continue
        seen.append(key)
        seen_names.append(name)
        kept.append((name, values))

    nb_dup = len(column_names) - len(kept)
    if nb_dup:
        print(f"Colonnes dupliquées fusionnées : {nb_dup} (sur {len(column_names)})")

    # Résolution des doublons de NOM (rare, mais on garantit l'unicité)
    used = {}
    final = []
    for name, values in kept:
        if name in used:
            used[name] += 1
            name = f"{name} ({used[name]})"
        else:
            used[name] = 1
        final.append((name, values))

    return final


def short_worksheet_name(title: str, existing_names: set) -> str:
    """Dérive un nom court et lisible de feuille de tracé à partir du TITLE
    d'origine (texte après le dernier ':' et avant la première ',')."""
    part = title.rsplit(": ", 1)[-1]
    part = part.split(",", 1)[0].strip()
    if not part:
        part = title.strip() or "Graphique"
    name = part[0].upper() + part[1:] if part else part

    base = name
    suffix = 1
    while name in existing_names:
        suffix += 1
        name = f"{base} ({suffix})"
    existing_names.add(name)
    return name


# --------------------------------------------------------------------------- #
# Génération des blocs XML LabPlot
# --------------------------------------------------------------------------- #

def lp_time(dt=None) -> str:
    """Formatte un horodatage au format utilisé par LabPlot dans ses fichiers
    de projet ('YYYY-DD-MM HH:MM:SS:mmm')."""
    dt = dt or datetime.datetime.now()
    return dt.strftime("%Y-%d-%m %H:%M:%S:") + f"{dt.microsecond // 1000:03d}"


def new_uuid() -> str:
    return "{" + str(uuid.uuid4()) + "}"


def encode_column_data(values) -> str:
    """Encode une liste de float en base64 (tableau brut de double
    little-endian, 8 octets par valeur — format natif LabPlot)."""
    raw = struct.pack(f"<{len(values)}d", *values)
    return base64.b64encode(raw).decode("ascii")


def build_column_xml(name: str, values) -> str:
    ct = lp_time()
    b64 = encode_column_data(values)
    return f"""            <column creation_time="{ct}" name={xml_attr(name)} uuid="{new_uuid()}" rows="{len(values)}" designation="0" mode="0" width="176">
                <comment>numerical data, {len(values)} elements</comment>
                <input_filter>
                    <simple_filter creation_time="{ct}" name="SimpleFilter" uuid="{new_uuid()}" filter_name="String2DoubleFilter">
                        <comment></comment>
                    </simple_filter>
                </input_filter>
                <output_filter>
                    <simple_filter creation_time="{ct}" name="SimpleFilter" uuid="{new_uuid()}" format="g" digits="6" filter_name="Double2StringFilter">
                        <comment></comment>
                    </simple_filter>
                </output_filter>{b64}</column>
"""


def build_spreadsheet_xml(project_name: str, columns) -> str:
    ct = lp_time()
    cols_xml = "".join(build_column_xml(name, values) for name, values in columns)
    return f"""    <child_aspect>
        <spreadsheet creation_time="{ct}" name="Feuille de calcul" uuid="{new_uuid()}">
            <comment></comment>
            <general showComments="0" showSparklines="0"/>
            <linking enabled="0" spreadsheet=""/>
{cols_xml}        </spreadsheet>
    </child_aspect>
"""


def rich_text(label: str) -> str:
    """Génère le petit fragment HTML riche utilisé par LabPlot pour le texte
    des titres d'axes."""
    if not label:
        return ""
    html = (
        '&lt;!DOCTYPE HTML PUBLIC &quot;-//W3C//DTD HTML 4.0//EN&quot; '
        '&quot;http://www.w3.org/TR/REC-html40/strict.dtd&quot;&gt;'
        '&lt;html&gt;&lt;head&gt;&lt;meta name=&quot;qrichtext&quot; content=&quot;1&quot; /&gt;'
        '&lt;meta charset=&quot;utf-8&quot; /&gt;&lt;style type=&quot;text/css&quot;&gt;\n'
        'p, li { white-space: pre-wrap; }\nhr { height: 1px; border-width: 0; }\n'
        'li.unchecked::marker { content: &quot;\\2610&quot;; }\n'
        'li.checked::marker { content: &quot;\\2612&quot;; }\n'
        "&lt;/style&gt;&lt;/head&gt;&lt;body style=&quot; font-family:'Segoe UI'; "
        'font-size:9pt; font-weight:400; font-style:normal;&quot;&gt;\n'
        '&lt;p style=&quot; margin-top:0px; margin-bottom:0px; margin-left:0px; '
        'margin-right:0px; -qt-block-indent:0; text-indent:0px;&quot;&gt;'
        f'&lt;span style=&quot; color:#000000; background-color:transparent;&quot;&gt;{xml_escape(label)}'
        '&lt;/span&gt;&lt;/p&gt;&lt;/body&gt;&lt;/html&gt;'
    )
    return html


def build_axis_xml(axis_name: str, orientation: int, position: int, is_primary: bool,
                    label_text: str, rotation: int) -> str:
    ct = lp_time()
    label_geom = 'x="51.6875" y="462.389"' if (is_primary and orientation == 0) else \
                 'x="-472.014" y="-50"' if (is_primary and orientation == 1) else 'x="0" y="0"'
    labels_position = "2" if is_primary else "0"
    major_grid_style = "1" if is_primary else "0"
    text_html = rich_text(label_text)
    return f"""                <axis creation_time="{ct}" name="{axis_name}" uuid="{new_uuid()}">
                    <comment></comment>
                    <general rangeType="0" orientation="{orientation}" position="{position}" scale="0" rangeScale="1" offset="0" logicalPosition="0" scaleRange="0" start="0" end="1" majorTicksStartType="1" majorTickStartOffset="0" majorTickStartValue="0" scalingFactor="1" zeroOffset="0" showScaleOffset="1" titleOffsetX="0" titleOffsetY="0" plotRangeIndex="0" visible="1"/>
                    <textLabel creation_time="{ct}" name="{axis_name}" uuid="{new_uuid()}">
                        <comment></comment>
                        <geometry {label_geom} horizontalPosition="1" verticalPosition="1" horizontalAlignment="1" verticalAlignment="1" rotationAngle="{rotation}" plotRangeIndex="0" visible="1" coordinateBinding="0" logicalPosX="0" logicalPosY="0" locked="0"/>
                        <text>{text_html}</text>
                        <format placeholder="0" mode="0" fontFamily="Computer Modern" fontSize="-1" fontPointSize="12" fontWeight="400" fontItalic="0" fontColor_r="0" fontColor_g="0" fontColor_b="0" backgroundColor_r="0" backgroundColor_g="0" backgroundColor_b="0"/>
                        <border borderShape="0" style="1" color_r="0" color_g="0" color_b="0" width="1" opacity="1"/>
                    </textLabel>
                    <line style="1" color_r="0" color_g="0" color_b="0" width="3.52778" opacity="1" arrowType="0" arrowPosition="1" arrowSize="35.2778"/>
                    <majorTicks direction="1" type="0" numberAuto="1" number="5" increment="0" majorTicksColumn="" length="21.1667" style="1" color_r="0" color_g="0" color_b="0" width="3.52778" opacity="1"/>
                    <minorTicks direction="1" type="0" numberAuto="1" number="1" increment="0" minorTicksColumn="" length="10.5833" style="1" color_r="0" color_g="0" color_b="0" width="3.52778" opacity="1"/>
                    <labels position="{labels_position}" offset="17.6389" rotation="0" textType="0" labelsTextColumn="" format="0" formatAuto="1" precision="1" autoPrecision="1" dateTimeFormat="yyyy-MM-dd hh:mm:ss" color_r="0" color_g="0" color_b="0" fontFamily="Segoe UI" fontSize="-1" fontPointSize="35.2778" fontWeight="400" fontItalic="0" prefix="" suffix="" opacity="1" backgroundType="0" backgroundColor_r="255" backgroundColor_g="255" backgroundColor_b="255"/>
                    <majorGrid style="{major_grid_style}" color_r="160" color_g="160" color_b="164" width="0" opacity="1"/>
                    <minorGrid style="0" color_r="160" color_g="160" color_b="164" width="0" opacity="1"/>
                </axis>
"""


def build_curve_xml(curve_name: str, x_path: str, y_path: str) -> str:
    ct = lp_time()
    return f"""                <xyCurve creation_time="{ct}" name={xml_attr(curve_name)} uuid="{new_uuid()}">
                    <comment></comment>
                    <general xColumn={xml_attr(x_path)} yColumn={xml_attr(y_path)} plotRangeIndex="0" legendVisible="1" visible="1"/>
                    <lines type="1" skipGaps="0" increasingXOnly="0" interpolationPointsCount="1" style="1" color_r="28" color_g="113" color_b="216" width="3.52778" opacity="1"/>
                    <dropLines type="0" style="1" color_r="28" color_g="113" color_b="216" width="3.52778" opacity="1"/>
                    <symbols symbolsStyle="0" opacity="1" rotation="0" size="17.6389" brush_style="1" brush_color_r="28" brush_color_g="113" brush_color_b="216" style="1" color_r="28" color_g="113" color_b="216" width="0"/>
                    <values type="0" valuesColumn="" position="0" distance="17.6389" rotation="0" opacity="1" numericFormat="f" dateTimeFormat="yyyy-MM-dd" precision="2" prefix="" suffix="" color_r="28" color_g="113" color_b="216" fontFamily="Segoe UI" fontSize="-1" fontPointSize="28.2222" fontWeight="400" fontItalic="0"/>
                    <filling position="0" type="0" colorStyle="0" imageStyle="1" brushStyle="1" firstColor_r="255" firstColor_g="255" firstColor_b="255" secondColor_r="0" secondColor_g="0" secondColor_b="0" fileName="" opacity="1"/>
                    <errorBars xErrorType="0" xErrorPlusColumn="" xErrorMinusColumn="" yErrorType="0" yErrorPlusColumn="" yErrorMinusColumn="" type="0" capSize="35.2778" style="1" color_r="28" color_g="113" color_b="216" width="3.52778" opacity="1"/>
                    <margins rugEnabled="0" rugOrientation="2" rugLength="17.6389" rugWidth="0" rugOffset="0"/>
                </xyCurve>
"""


def build_worksheet_xml(ws_name: str, plot_name: str, curve_name: str,
                         x_path: str, y_path: str,
                         x_label: str, y_label: str) -> str:
    ct = lp_time()
    axes_xml = (
        build_axis_xml("x", 0, 1, True, x_label, 0)
        + build_axis_xml("x2", 0, 0, False, "", 0)
        + build_axis_xml("y", 1, 2, True, y_label, -90)
        + build_axis_xml("y2", 1, 3, False, "", -90)
    )
    curve_xml = build_curve_xml(curve_name, x_path, y_path)
    return f"""    <child_aspect>
        <worksheet creation_time="{ct}" name={xml_attr(ws_name)} uuid="{new_uuid()}">
            <comment></comment>
            <geometry x="0" y="0" width="1200" height="1000" useViewSize="0" zoomFit="1"/>
            <layout layout="1" topMargin="0" bottomMargin="0" leftMargin="0" rightMargin="0" verticalSpacing="0" horizontalSpacing="0" columnCount="2" rowCount="2"/>
            <background type="0" colorStyle="0" imageStyle="1" brushStyle="1" firstColor_r="255" firstColor_g="255" firstColor_b="255" secondColor_r="0" secondColor_g="0" secondColor_b="0" fileName="" opacity="1"/>
            <plotProperties plotInteractive="1" cartesianPlotActionMode="0" cartesianPlotCursorMode="1"/>
            <cartesianPlot creation_time="{ct}" name={xml_attr(plot_name)} uuid="{new_uuid()}">
                <comment></comment>
                <cursor style="1" color_r="255" color_g="0" color_b="0" width="3.52778" opacity="1"/>
                <geometry x="0" y="0" width="1000" height="1000" visible="1"/>
                <xRanges>
                    <xRange autoScale="1" start="0" end="1" scale="0" format="0" dateTimeFormat="yyyy-MM-dd hh:mm:ss"/>
                </xRanges>
                <yRanges>
                    <yRange autoScale="1" start="0" end="1" scale="0" format="0" dateTimeFormat="yyyy-MM-dd hh:mm:ss"/>
                </yRanges>
                <coordinateSystems defaultCoordinateSystem="0" horizontalPadding="153.375" verticalPadding="50" rightPadding="50" bottomPadding="150" symmetricPadding="0" rangeType="0" rangeFirstValues="1000" rangeLastValues="1000" niceExtend="1">
                    <coordinateSystem name="" xIndex="0" yIndex="0"/>
                </coordinateSystems>
                <xRangeBreaks enabled="0">
                    <xRangeBreak start="nan" end="nan" position="0.5" style="2"/>
                </xRangeBreaks>
                <yRangeBreaks enabled="0">
                    <yRangeBreak start="nan" end="nan" position="0.5" style="2"/>
                </yRangeBreaks>
                <plotArea creation_time="{ct}" name="Plot Area - Feuille de calcul plot area" uuid="{new_uuid()}">
                    <comment></comment>
                    <background type="0" colorStyle="0" imageStyle="1" brushStyle="1" firstColor_r="255" firstColor_g="255" firstColor_b="255" secondColor_r="0" secondColor_g="0" secondColor_b="0" fileName="" opacity="1"/>
                    <border borderType="0" style="1" color_r="0" color_g="0" color_b="0" width="3.52778" opacity="1" borderCornerRadius="0"/>
                </plotArea>
                <textLabel creation_time="{ct}" name="Plot Area - Feuille de calcul - Title" uuid="{new_uuid()}">
                    <comment></comment>
                    <geometry x="0" y="0" horizontalPosition="1" verticalPosition="0" horizontalAlignment="1" verticalAlignment="0" rotationAngle="0" plotRangeIndex="0" visible="1" coordinateBinding="0" logicalPosX="0" logicalPosY="0" locked="0"/>
                    <text></text>
                    <format placeholder="0" mode="0" fontFamily="Computer Modern" fontSize="-1" fontPointSize="12" fontWeight="400" fontItalic="0" fontColor_r="0" fontColor_g="0" fontColor_b="0" backgroundColor_r="1" backgroundColor_g="1" backgroundColor_b="1"/>
                    <border borderShape="0" style="1" color_r="0" color_g="0" color_b="0" width="1" opacity="1"/>
                </textLabel>
{axes_xml}{curve_xml}            </cartesianPlot>
        </worksheet>
    </child_aspect>
"""


def build_state_xml(project_name: str, worksheet_names) -> str:
    lines = [f'        <expanded path={xml_attr(project_name)}/>']
    lines.append(
        f'        <view path={xml_attr(project_name + "/Feuille de calcul")} state="0" x="0" y="21" width="909" height="890"/>'
    )
    for ws in worksheet_names:
        path = f"{project_name}/{ws}"
        lines.append(f'        <view path={xml_attr(path)} state="0" x="0" y="21" width="909" height="890"/>')
        lines.append(f'        <expanded path={xml_attr(path)}/>')
    body = "\n".join(lines)
    return f"    <state>\n{body}\n    </state>\n"


def build_project_xml(input_path: Path, graphs_meta, spreadsheet_columns) -> str:
    project_name = "Projet"
    ct = lp_time()

    spreadsheet_xml = build_spreadsheet_xml(project_name, spreadsheet_columns)
    spreadsheet_col_names = {name for name, _ in spreadsheet_columns}

    # La colonne de temps est celle utilisée en abscisse pour tous les graphiques
    # (première colonne conservée après déduplication qui correspond à un "Temps ...")
    time_col_name = None
    for name, _ in spreadsheet_columns:
        if name.startswith("Temps"):
            time_col_name = name
            break
    if time_col_name is None:
        time_col_name = spreadsheet_columns[0][0]

    existing_ws_names = set()
    worksheets_xml = []
    worksheet_names = []

    used_value_col_names = iter(name for name, _ in spreadsheet_columns if name != time_col_name)

    for meta in graphs_meta:
        title = meta.get("TITLE", "")
        xunit = meta.get("XUNIT", "")
        yunit = meta.get("YUNIT", "")
        y_label = f"{title} [{yunit}]" if yunit else (title or "Valeur")
        x_label = f"Temps [{xunit}]" if xunit else "Temps"

        # Nom de colonne de mesure attendu pour ce graphique (peut avoir été
        # renommé en cas de doublon de nom -> on retrouve la bonne colonne
        # par correspondance sur le nom construit, avec repli sur l'ordre)
        y_col_name = y_label if y_label in spreadsheet_col_names else next(used_value_col_names, y_label)

        ws_name = short_worksheet_name(title, existing_ws_names)
        worksheet_names.append(ws_name)

        x_path = f"{project_name}/Feuille de calcul/{time_col_name}"
        y_path = f"{project_name}/Feuille de calcul/{y_col_name}"

        worksheets_xml.append(
            build_worksheet_xml(ws_name, ws_name, y_col_name, x_path, y_path, x_label, y_label)
        )

    state_xml = build_state_xml(project_name, worksheet_names)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE LabPlotXML>
<project version="2.12.1" xmlVersion="16" modificationTime={xml_attr(ct)} author="" saveCalculations="1" saveDefaultDockWidgetState="0" creation_time={xml_attr(ct)} name={xml_attr(project_name)} uuid="{new_uuid()}">
    <comment></comment>
{spreadsheet_xml}{''.join(worksheets_xml)}{state_xml}</project>
"""
    return xml


# --------------------------------------------------------------------------- #
# Écriture du fichier .lml (XML compressé XZ)
# --------------------------------------------------------------------------- #

def write_lml(xml_text: str, output_path: Path):
    data = xml_text.encode("utf-8")
    compressed = lzma.compress(data, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC32)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(compressed)


def main():
    parser = argparse.ArgumentParser(
        description="Convertit un CSV multi-graphiques en projet LabPlot (.lml)."
    )
    parser.add_argument("input_csv", type=Path, help="Chemin du fichier CSV source")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Chemin du fichier .lml de sortie (par défaut : <nom_fichier>.lml à côté du fichier source)",
    )
    args = parser.parse_args()

    input_path = args.input_csv
    if not input_path.exists():
        sys.exit(f"Erreur : fichier introuvable : {input_path}")

    output_path = args.output or input_path.with_suffix(".lml")

    graphs_meta, columns_values = load_graphs(input_path)
    spreadsheet_columns = build_spreadsheet_columns(graphs_meta, columns_values)

    xml_text = build_project_xml(input_path, graphs_meta, spreadsheet_columns)
    write_lml(xml_text, output_path)

    print(f"Terminé : projet LabPlot écrit dans '{output_path}' "
          f"({len(spreadsheet_columns)} colonnes, {len(graphs_meta)} graphiques).")


if __name__ == "__main__":
    main()
