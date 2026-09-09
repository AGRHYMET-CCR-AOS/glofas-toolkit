"""Visualisation interactive des résultats de ``glofas_extract.py``.

Ce module transforme les sorties de l'extraction (séries temporelles +
résumé du recalage par point) en trois livrables consultables dans un
navigateur, sans dépendance à Python à l'ouverture :

- une **carte interactive** (point saisi, maille GloFAS retenue, rayon de
  recherche, code couleur selon la qualité du recalage) ;
- un **graphique temporel interactif** (une courbe par point, zoom, survol) ;
- un **rapport HTML autonome** combinant les deux, plus un tableau récapitulatif.

Prérequis :
    python -m pip install folium plotly
    (ou, dans l'environnement Conda de l'atelier :
     conda install -c conda-forge folium plotly)

Utilisation typique, à la suite de ``glofas_extract.extract_glofas_at_points`` ::

    from glofas_extract import extract_glofas_at_points

    series = extract_glofas_at_points(
        points="points.csv", input_dir="glofas_data",
        start="1980-01", end="1983-12", radius_km=10,
        output="resultats/extraction", make_report=True,
    )
    # -> resultats/extraction_carte.html
    # -> resultats/extraction_series.html
    # -> resultats/extraction_rapport.html

Ou directement à partir des CSV déjà écrits ::

    python glofas_visualize.py \
        --series resultats/extraction_series.csv \
        --points resultats/extraction_points.csv \
        --output resultats/extraction
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

import pandas as pd

LOG = logging.getLogger("glofas_visualize")

# --------------------------------------------------------------------------- #
# Palette (cohérente avec les autres livrables du dépôt)
# --------------------------------------------------------------------------- #

# Couleurs de statut : jamais réutilisées comme couleurs de série, toujours
# accompagnées d'une étiquette (jamais la couleur seule qui porte le sens).
STATUS_COLORS = {
    "ok": "#0ca30c",
    "recale": "#fab219",
    "repli": "#d03b3b",
}
STATUS_LABELS = {
    "ok": "Maille la plus proche (pas de recalage nécessaire)",
    "recale": "Recalé dans le rayon de recherche",
    "repli": "Repli : aucune maille valide dans le rayon",
}
STATUS_ORDER = ["ok", "recale", "repli"]

# Palette "risque" (4 niveaux -- good/warning/serious/critical du skill
# dataviz), utilisée par build_risk_map / interactive_risk_explorer.
RISK_COLORS = {
    "normal": "#0ca30c",
    "risque_faible": "#fab219",
    "risque_modere": "#ec835a",
    "risque_severe": "#d03b3b",
}

# Couleurs catégorielles (ordre fixe) pour les séries temporelles.
CATEGORICAL_PALETTE = [
    "#2a78d6",  # bleu
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # jaune
    "#e87ba4",  # magenta
    "#008300",  # vert
    "#4a3aa7",  # violet
    "#e34948",  # rouge
]


def _load_table(source: str | Path | pd.DataFrame) -> pd.DataFrame:
    if isinstance(source, pd.DataFrame):
        return source.copy()
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"Fichier introuvable : {path}")
    return pd.read_csv(path)


def _points_status(points_meta: pd.DataFrame) -> pd.DataFrame:
    """Ajoute une colonne 'statut' (ok/recale/repli) si absente, et déduplique
    sur 'id' (au cas où un DataFrame de séries complet, avec une ligne par
    date, serait passé par erreur à la place du résumé par point).
    """
    meta = points_meta.drop_duplicates("id").reset_index(drop=True)
    if "statut" not in meta.columns:
        fallback_col = (
            meta["repli_sur_plus_proche"] if "repli_sur_plus_proche" in meta.columns else pd.Series(False, index=meta.index)
        )
        moved_col = meta["recale"] if "recale" in meta.columns else pd.Series(False, index=meta.index)
        meta["statut"] = [
            "repli" if bool(f) else ("recale" if bool(m) else "ok") for f, m in zip(fallback_col, moved_col)
        ]
    missing = {"id", "lon_input", "lat_input", "lon_pixel", "lat_pixel", "distance_km"} - set(meta.columns)
    if missing:
        raise ValueError(
            f"Colonnes manquantes dans le résumé des points : {sorted(missing)}. "
            "Utilisez le fichier '*_points.csv' produit par extract_glofas_at_points."
        )
    return meta


# --------------------------------------------------------------------------- #
# Carte interactive
# --------------------------------------------------------------------------- #


def build_points_map(
    points_meta: str | Path | pd.DataFrame,
    output_html: str | Path | None = None,
    *,
    zoom_start: int = 8,
):
    """Construit une carte Folium (Leaflet) des points extraits.

    Pour chaque point : un petit marqueur pour la coordonnée saisie par
    l'utilisateur, un marqueur pour la maille GloFAS effectivement retenue,
    un trait les reliant, et — si un rayon de recherche a été utilisé — un
    cercle matérialisant ce rayon. La couleur indique la qualité du
    recalage (voir ``STATUS_LABELS``) ; les points sont regroupés par statut
    dans des calques activables/désactivables.
    """
    import folium

    meta = _points_status(_load_table(points_meta))
    if meta.empty:
        raise ValueError("Aucun point à afficher sur la carte.")

    center = [float(meta["lat_input"].mean()), float(meta["lon_input"].mean())]
    fmap = folium.Map(location=center, zoom_start=zoom_start, tiles="OpenStreetMap", control_scale=True)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri, Maxar, Earthstar Geographics",
        name="Satellite (Esri)",
    ).add_to(fmap)

    groups = {status: folium.FeatureGroup(name=STATUS_LABELS[status]) for status in STATUS_ORDER}

    for row in meta.itertuples():
        status = row.statut if row.statut in STATUS_COLORS else "ok"
        color = STATUS_COLORS[status]
        group = groups[status]

        radius_km = float(getattr(row, "radius_km", 0) or 0)
        method = getattr(row, "method", "nearest")
        popup_html = (
            f"<b>{row.id}</b><br>"
            f"Statut : {STATUS_LABELS[status]}<br>"
            f"Point saisi : {row.lat_input:.5f}, {row.lon_input:.5f}<br>"
            f"Maille retenue : {row.lat_pixel:.5f}, {row.lon_pixel:.5f}<br>"
            f"Distance : {row.distance_km:.2f} km<br>"
            f"Méthode : {method} (rayon {radius_km:g} km)"
        )
        popup = folium.Popup(popup_html, max_width=320)

        if radius_km > 0:
            folium.Circle(
                location=[row.lat_input, row.lon_input],
                radius=radius_km * 1000,
                color=color,
                weight=1,
                fill=False,
                dash_array="4,4",
                opacity=0.6,
            ).add_to(group)

        folium.PolyLine(
            locations=[[row.lat_input, row.lon_input], [row.lat_pixel, row.lon_pixel]],
            color=color,
            weight=2,
            opacity=0.85,
            dash_array="6,4",
        ).add_to(group)

        folium.CircleMarker(
            location=[row.lat_input, row.lon_input],
            radius=5,
            color="#33322e",
            weight=1,
            fill=True,
            fill_color="#faf9f7",
            fill_opacity=1.0,
            tooltip=f"{row.id} — point saisi",
            popup=popup,
        ).add_to(group)

        folium.CircleMarker(
            location=[row.lat_pixel, row.lon_pixel],
            radius=7,
            color=color,
            weight=2,
            fill=True,
            fill_color=color,
            fill_opacity=0.9,
            tooltip=f"{row.id} — maille retenue ({row.distance_km:.2f} km)",
            popup=popup,
        ).add_to(group)

    for group in groups.values():
        group.add_to(fmap)
    folium.LayerControl(collapsed=False).add_to(fmap)

    legend_items = "".join(
        f'<div style="display:flex;align-items:center;gap:6px;margin:2px 0;">'
        f'<span style="width:10px;height:10px;border-radius:50%;background:{STATUS_COLORS[s]};'
        f'display:inline-block;"></span>{STATUS_LABELS[s]}</div>'
        for s in STATUS_ORDER
    )
    legend_html = f"""
    <div style="position: fixed; bottom: 24px; left: 24px; z-index: 9999;
                background: rgba(255,255,255,0.95); padding: 10px 14px;
                border-radius: 8px; border: 1px solid #ccc; font-size: 12px;
                font-family: system-ui, sans-serif; box-shadow: 0 1px 4px rgba(0,0,0,0.15);">
        <div style="font-weight:600;margin-bottom:4px;">Recalage spatial</div>
        {legend_items}
        <div style="margin-top:0;color:#666;">● cercle plein = point saisi &nbsp;|&nbsp; ● grand cercle = maille retenue</div>
    </div>
    """
    fmap.get_root().html.add_child(folium.Element(legend_html))

    if output_html is not None:
        output_path = Path(output_html)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fmap.save(str(output_path))
        LOG.info("Carte enregistrée : %s", output_path)

    return fmap


# --------------------------------------------------------------------------- #
# Graphique temporel interactif
# --------------------------------------------------------------------------- #


def _id_color_map(all_ids: Sequence[str]) -> dict[str, str]:
    """Associe une couleur fixe à chaque identifiant (ordre catégoriel fixe),
    pour qu'une station garde toujours la même couleur, y compris quand on
    n'en affiche qu'un sous-ensemble.
    """
    return {str(point_id): CATEGORICAL_PALETTE[i % len(CATEGORICAL_PALETTE)] for i, point_id in enumerate(all_ids)}


def build_timeseries_figure(
    series: str | Path | pd.DataFrame,
    output_html: str | Path | None = None,
    *,
    ids: Sequence[str] | None = None,
    start: str | date | None = None,
    end: str | date | None = None,
    title: str | None = None,
    color_map: dict[str, str] | None = None,
):
    """Construit un graphique Plotly interactif (une courbe par point).

    ``ids`` restreint l'affichage à une liste de stations (toutes par
    défaut) ; ``start``/``end`` restreignent la période affichée (format
    ``'YYYY'``, ``'YYYY-MM'``, ``'YYYY-MM-DD'`` ou un ``datetime.date`` — par
    défaut, toute la période disponible dans ``series``). ``color_map``
    permet d'imposer une couleur fixe par station (voir
    ``interactive_timeseries_explorer``, qui l'utilise pour qu'une station
    garde sa couleur quelle que soit la sélection courante) ; sinon la
    couleur de chaque station est déduite de son ordre d'apparition dans
    ``series``.
    """
    import plotly.graph_objects as go

    data = _load_table(series)
    if data.empty:
        raise ValueError("Aucune donnée à tracer.")
    data = data.copy()
    data["date"] = pd.to_datetime(data["date"])

    palette = color_map or _id_color_map(dict.fromkeys(data["id"]))

    if ids:
        keep = {str(i) for i in ids}
        data = data[data["id"].astype(str).isin(keep)]
    if start is not None:
        data = data[data["date"] >= pd.Timestamp(start)]
    if end is not None:
        data = data[data["date"] <= pd.Timestamp(end)]
    if data.empty:
        raise ValueError("Aucune donnée à tracer pour cette sélection (stations et/ou période).")

    selected_ids = list(dict.fromkeys(data["id"]))  # ordre d'apparition, sans doublons
    variable = str(data["variable"].iloc[0]) if "variable" in data.columns else "débit"

    fig = go.Figure()
    for point_id in selected_ids:
        color = palette.get(str(point_id), CATEGORICAL_PALETTE[0])
        subset = data[data["id"] == point_id].sort_values("date")
        fig.add_trace(
            go.Scatter(
                x=subset["date"],
                y=subset["value"],
                mode="lines",
                name=str(point_id),
                line=dict(color=color, width=2),
                hovertemplate="%{x|%Y-%m-%d} : %{y:.2f} m³/s<extra>" + str(point_id) + "</extra>",
            )
        )

    fig.update_layout(
        title=title or f"Débit GloFAS extrait — {variable}",
        xaxis_title="Date",
        yaxis_title="Débit (m³/s)",
        hovermode="x unified",
        legend_title_text="Point",
        template="plotly_white",
        margin=dict(l=60, r=20, t=60, b=40),
    )
    fig.update_xaxes(rangeslider_visible=True)

    if output_html is not None:
        output_path = Path(output_html)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # 'inline' embarque la librairie Plotly dans le fichier : le graphique
        # reste consultable hors ligne (utile en atelier, connexion incertaine).
        fig.write_html(str(output_path), include_plotlyjs="inline")
        LOG.info("Graphique enregistré : %s", output_path)

    return fig


# --------------------------------------------------------------------------- #
# Explorateur interactif (widgets Jupyter)
# --------------------------------------------------------------------------- #


def interactive_timeseries_explorer(
    series: str | Path | pd.DataFrame,
    *,
    default_output: str | Path = "resultats/extraction_series_perso.html",
):
    """Affiche, dans un notebook Jupyter, un petit tableau de bord pour
    choisir les stations et la période à tracer, et enregistrer le
    graphique obtenu.

    Nécessite ``ipywidgets`` (inclus dans l'environnement Conda de
    l'atelier ; sans effet en dehors d'un notebook Jupyter — utilisez
    directement ``build_timeseries_figure(..., ids=..., start=..., end=...)``
    dans un script).
    """
    try:
        import ipywidgets as widgets
    except ImportError as exc:
        raise RuntimeError(
            "ipywidgets est requis pour l'explorateur interactif : "
            "pip install ipywidgets (ou conda install -c conda-forge ipywidgets)"
        ) from exc
    from IPython.display import display

    data = _load_table(series)
    if data.empty:
        raise ValueError("Aucune donnée à explorer.")
    data = data.copy()
    data["date"] = pd.to_datetime(data["date"])

    all_ids = list(dict.fromkeys(data["id"]))
    palette = _id_color_map(all_ids)
    min_date, max_date = data["date"].min().date(), data["date"].max().date()

    ids_picker = widgets.SelectMultiple(
        options=all_ids,
        value=tuple(all_ids),
        description="Stations",
        rows=min(max(len(all_ids), 3), 8),
        layout=widgets.Layout(width="260px"),
    )
    start_picker = widgets.DatePicker(description="Début", value=min_date)
    end_picker = widgets.DatePicker(description="Fin", value=max_date)
    reset_button = widgets.Button(description="Tout sélectionner", icon="refresh", layout=widgets.Layout(width="160px"))
    filename_box = widgets.Text(value=str(default_output), description="Fichier", layout=widgets.Layout(width="420px"))
    save_button = widgets.Button(description="Enregistrer le graphique", icon="save", button_style="success")
    save_status = widgets.Output()
    chart_output = widgets.Output()

    def _current_selection():
        selected_ids = list(ids_picker.value) or all_ids
        start = start_picker.value or min_date
        end = end_picker.value or max_date
        return selected_ids, start, end

    def render(*_change):
        selected_ids, start, end = _current_selection()
        with chart_output:
            chart_output.clear_output(wait=True)
            try:
                fig = build_timeseries_figure(data, ids=selected_ids, start=start, end=end, color_map=palette)
            except ValueError as exc:
                print(exc)
                return
            fig.show()

    def on_reset_clicked(_button):
        ids_picker.value = tuple(all_ids)
        start_picker.value = min_date
        end_picker.value = max_date

    def on_save_clicked(_button):
        selected_ids, start, end = _current_selection()
        with save_status:
            save_status.clear_output(wait=True)
            try:
                fig = build_timeseries_figure(data, ids=selected_ids, start=start, end=end, color_map=palette)
                output_path = Path(filename_box.value)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                fig.write_html(str(output_path), include_plotlyjs="inline")
                print(f"Graphique enregistré : {output_path}")
            except ValueError as exc:
                print(exc)

    ids_picker.observe(render, names="value")
    start_picker.observe(render, names="value")
    end_picker.observe(render, names="value")
    reset_button.on_click(on_reset_clicked)
    save_button.on_click(on_save_clicked)

    controls = widgets.VBox(
        [
            widgets.HBox([ids_picker, widgets.VBox([start_picker, end_picker, reset_button])]),
            widgets.HBox([filename_box, save_button]),
            save_status,
        ]
    )
    display(controls, chart_output)
    render()


# --------------------------------------------------------------------------- #
# Carte de risque (prévision classée par glofas_risk.classify_forecast_risk)
# --------------------------------------------------------------------------- #


def _zone_geometry(zone):
    """Normalise ``zone`` (voir ``build_risk_map``) en géométrie shapely, ou
    ``None``. Accepte un ``glofas_basins.Zone``, un GeoDataFrame/GeoSeries
    (geopandas), ou directement une géométrie shapely.
    """
    if zone is None:
        return None
    type_name = type(zone).__name__
    if type_name in ("GeoDataFrame", "GeoSeries"):
        return zone.union_all() if hasattr(zone, "union_all") else zone.unary_union
    if hasattr(zone, "geometry") and not hasattr(zone, "geom_type"):
        # p. ex. glofas_basins.Zone : dataclass avec un attribut .geometry (shapely)
        return zone.geometry
    return zone  # déjà une géométrie shapely


def build_risk_map(
    risk_table: str | Path | pd.DataFrame,
    points_meta: str | Path | pd.DataFrame,
    output_html: str | Path | None = None,
    *,
    issue_date: str | None = None,
    leadtime_hours: int | None = None,
    zone=None,
    zoom_start: int = 8,
):
    """Construit une carte Folium des points colorés par catégorie de risque,
    pour une date d'émission et une échéance données.

    ``risk_table`` : sortie de ``glofas_risk.classify_forecast_risk``.
    ``points_meta`` : ``*_points.csv`` de l'extraction historique (pour les
    coordonnées -- même fichier que celui passé à
    ``glofas_forecast.extract_glofas_forecast_at_points``).
    ``zone`` (optionnel) : zone d'étude à afficher en contour et à laquelle
    la carte est cadrée (``fit_bounds``) -- accepte un ``glofas_basins.Zone``
    (issu de ``glofas_basins.resolve_zone``), un GeoDataFrame/GeoSeries
    geopandas, ou directement une géométrie shapely. Sans ``zone``, la carte
    est centrée sur la moyenne des points affichés (comme avant).

    Si plusieurs dates d'émission sont présentes dans ``risk_table`` et
    qu'``issue_date`` n'est pas précisé, la plus récente est utilisée. En
    revanche, si plusieurs échéances sont présentes, ``leadtime_hours`` doit
    être précisé explicitement (pas de choix implicite pour l'échéance,
    contrairement à la date d'émission) -- utilisez
    ``interactive_risk_explorer`` en notebook, ou ``generate_daily_risk_maps``
    pour produire automatiquement une carte par échéance, au lieu d'appeler
    cette fonction à la main pour chaque échéance.
    """
    import folium

    from glofas_risk import RISK_LABELS, RISK_ORDER

    risk = _load_table(risk_table)
    meta = _load_table(points_meta).drop_duplicates("id")
    if risk.empty:
        raise ValueError("Table de risque vide.")

    available_issues = sorted(risk["issue_date"].astype(str).unique())
    if issue_date is None:
        issue_date = available_issues[-1]
        if len(available_issues) > 1:
            LOG.info("issue_date non précisé : utilisation de la plus récente (%s)", issue_date)
    subset = risk[risk["issue_date"].astype(str) == str(issue_date)]
    if subset.empty:
        raise ValueError(f"Aucune donnée pour issue_date={issue_date!r}. Dates disponibles : {available_issues}")

    available_leadtimes = sorted(int(h) for h in subset["leadtime_hours"].unique())
    if leadtime_hours is None:
        if len(available_leadtimes) > 1:
            raise ValueError(
                f"Plusieurs échéances disponibles pour {issue_date} ({available_leadtimes} h) -- "
                "précisez leadtime_hours, ou utilisez interactive_risk_explorer pour un sélecteur interactif."
            )
        leadtime_hours = available_leadtimes[0]
    subset = subset[subset["leadtime_hours"] == leadtime_hours]
    if subset.empty:
        raise ValueError(f"Aucune donnée pour leadtime_hours={leadtime_hours} (disponibles : {available_leadtimes})")

    data = subset.merge(meta[["id", "lon_input", "lat_input", "lon_pixel", "lat_pixel"]], on="id", how="left")
    missing_coords = sorted(data.loc[data["lon_pixel"].isna(), "id"].unique())
    if missing_coords:
        LOG.warning("Points absents de points_meta (coordonnées manquantes), ignorés : %s", missing_coords)
        data = data.dropna(subset=["lon_pixel", "lat_pixel"])
    if data.empty:
        raise ValueError("Aucun point avec coordonnées connues à afficher.")

    center = [float(data["lat_pixel"].mean()), float(data["lon_pixel"].mean())]
    fmap = folium.Map(location=center, zoom_start=zoom_start, tiles="OpenStreetMap", control_scale=True)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri, Maxar, Earthstar Geographics",
        name="Satellite (Esri)",
    ).add_to(fmap)

    zone_geom = _zone_geometry(zone)
    if zone_geom is not None:
        from shapely.geometry import mapping as shapely_mapping

        folium.GeoJson(
            shapely_mapping(zone_geom),
            name="Zone d'étude",
            style_function=lambda _: {"color": "#333333", "weight": 2, "fill": False, "dashArray": "5 4"},
        ).add_to(fmap)
        minx, miny, maxx, maxy = zone_geom.bounds
        fmap.fit_bounds([[miny, minx], [maxy, maxx]])

    groups = {level: folium.FeatureGroup(name=RISK_LABELS[level]) for level in RISK_ORDER}
    threshold_cols = [c for c in data.columns if c.startswith("seuil_")]
    prob_cols = [c for c in data.columns if c.startswith("probabilite_depassement_")]

    for row in data.itertuples():
        level = row.risque if row.risque in RISK_COLORS else "normal"
        color = RISK_COLORS[level]

        seuils_txt = "".join(
            f"{c.replace('seuil_', 'Seuil ')} : {getattr(row, c):.1f} m³/s<br>"
            for c in threshold_cols
            if pd.notna(getattr(row, c, None))
        )
        prob_txt = "".join(
            f"P(dépasse {c.replace('probabilite_depassement_', '')}) : {getattr(row, c) * 100:.0f} %<br>"
            for c in prob_cols
            if pd.notna(getattr(row, c, None))
        )
        popup_html = (
            f"<b>{row.id}</b> — {RISK_LABELS[level]}<br>"
            f"Émission {issue_date}, échéance J+{int(row.leadtime_hours) // 24} ({int(row.leadtime_hours)} h)<br>"
            f"Statistique centrale : {row.membre_central:.1f} m³/s (contrôle : {row.valeur_controle:.1f})<br>"
            f"Étendue ensemble ({int(row.n_membres)} membres) : {row.minimum:.1f} — {row.maximum:.1f} m³/s<br>"
            f"{seuils_txt}{prob_txt}"
        )
        popup = folium.Popup(popup_html, max_width=340)

        folium.CircleMarker(
            location=[row.lat_pixel, row.lon_pixel],
            radius=8,
            color=color,
            weight=2,
            fill=True,
            fill_color=color,
            fill_opacity=0.9,
            tooltip=f"{row.id} — {RISK_LABELS[level]}",
            popup=popup,
        ).add_to(groups[level])

    for group in groups.values():
        group.add_to(fmap)
    folium.LayerControl(collapsed=False).add_to(fmap)

    legend_items = "".join(
        f'<div style="display:flex;align-items:center;gap:6px;margin:2px 0;">'
        f'<span style="width:10px;height:10px;border-radius:50%;background:{RISK_COLORS[l]};'
        f'display:inline-block;"></span>{RISK_LABELS[l]}</div>'
        for l in RISK_ORDER
    )
    legend_html = f"""
    <div style="position: fixed; bottom: 24px; left: 24px; z-index: 9999;
                background: rgba(255,255,255,0.95); padding: 10px 14px;
                border-radius: 8px; border: 1px solid #ccc; font-size: 12px;
                font-family: system-ui, sans-serif; box-shadow: 0 1px 4px rgba(0,0,0,0.15);">
        <div style="font-weight:600;margin-bottom:4px;">Risque d'inondation — émission {issue_date},
        échéance J+{int(leadtime_hours) // 24}</div>
        {legend_items}
        <div style="margin-top:4px;color:#666;">Seuils = quantiles historiques par point (glofas_risk.py)</div>
    </div>
    """
    fmap.get_root().html.add_child(folium.Element(legend_html))

    if output_html is not None:
        output_path = Path(output_html)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fmap.save(str(output_path))
        LOG.info("Carte de risque enregistrée : %s", output_path)

    return fmap


def generate_daily_risk_maps(
    risk_table: str | Path | pd.DataFrame,
    points_meta: str | Path | pd.DataFrame,
    output_dir: str | Path,
    *,
    issue_date: str | None = None,
    zone=None,
    zoom_start: int = 8,
) -> list[Path]:
    """Génère automatiquement une carte de risque par échéance journalière
    disponible, pour une date d'émission donnée (la plus récente si non
    précisé -- même règle que ``build_risk_map``).

    Une carte HTML est écrite par échéance (J+1, J+2, ... jusqu'à l'échéance
    maximale disponible dans ``risk_table``), nommée
    ``carte_risque_<issue_date>_Jplus<NN>.html`` dans ``output_dir``.
    ``zone`` (optionnel, voir ``build_risk_map``) délimite chaque carte à la
    zone d'étude -- typiquement le ``glofas_basins.Zone`` utilisé pour
    sélectionner les sous-bassins en amont (``glofas_basins.resolve_zone``).

    Retourne la liste des chemins écrits, dans l'ordre des échéances
    croissantes.
    """
    risk = _load_table(risk_table)
    if risk.empty:
        raise ValueError("Table de risque vide.")

    available_issues = sorted(risk["issue_date"].astype(str).unique())
    if issue_date is None:
        issue_date = available_issues[-1]
        if len(available_issues) > 1:
            LOG.info("issue_date non précisé : utilisation de la plus récente (%s)", issue_date)
    subset = risk[risk["issue_date"].astype(str) == str(issue_date)]
    if subset.empty:
        raise ValueError(f"Aucune donnée pour issue_date={issue_date!r}. Dates disponibles : {available_issues}")

    leadtimes = sorted(int(h) for h in subset["leadtime_hours"].unique())
    if not leadtimes:
        raise ValueError(f"Aucune échéance disponible pour issue_date={issue_date!r}.")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for hours in leadtimes:
        day = hours // 24
        out_file = output_path / f"carte_risque_{issue_date}_Jplus{day:02d}.html"
        build_risk_map(
            risk, points_meta, out_file,
            issue_date=issue_date, leadtime_hours=hours, zone=zone, zoom_start=zoom_start,
        )
        written.append(out_file)

    LOG.info(
        "Écrit %d carte(s) de risque journalières dans %s (émission %s, échéances %s h).",
        len(written), output_path, issue_date, leadtimes,
    )
    return written


# --------------------------------------------------------------------------- #
# Cartes de risque « statiques » (fond de carte imprimable, style bulletin
# CILSS/AGRHYMET) -- zones colorées plutôt que des marqueurs ponctuels, avec
# limites administratives, flèche du nord, barre d'échelle et légende.
#
# Prérequis supplémentaire : matplotlib (déjà présent avec la plupart des
# environnements Jupyter/geopandas ; sinon `pip install matplotlib`).
# --------------------------------------------------------------------------- #

_MOIS_FR = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def _format_date_fr(ts: pd.Timestamp) -> str:
    """Formate une date en français ('5 septembre 2026'), sans dépendre de
    la locale système (peu fiable sous Windows) ni du spécificateur '%-d'
    (non portable -- absent sous Windows)."""
    return f"{ts.day} {_MOIS_FR[ts.month - 1]} {ts.year}"


def _ensure_wgs84(gdf):
    """Reprojette en EPSG:4326 si besoin (avertit si le CRS n'est pas déclaré,
    et suppose alors WGS84 -- même convention que ``glofas_basins.resolve_zone``)."""
    if gdf.crs is None:
        LOG.warning("Couche sans CRS déclaré -- on suppose EPSG:4326 (WGS84).")
        return gdf.set_crs("EPSG:4326")
    if gdf.crs.to_epsg() != 4326:
        return gdf.to_crs("EPSG:4326")
    return gdf


def _country_basemap(countries_path: str | Path):
    import geopandas as gpd

    path = Path(countries_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"Fond de carte introuvable : {path} -- précisez countries_path "
            "(ex. 'static/afrique.gpkg')."
        )
    return _ensure_wgs84(gpd.read_file(path))


def _basin_geometries(basins, basin_id_col: str | None = None):
    """Normalise ``basins`` en GeoDataFrame à deux colonnes (``id`` en
    chaîne, ``geometry``), prêt à être fusionné avec une table de risque sur
    sa colonne ``id``.

    ``basins`` : GeoDataFrame de polygones de sous-bassins -- typiquement la
    variable ``selection`` renvoyée par
    ``glofas_basins.select_basins_in_zone`` ; ``basin_id_col`` précise la
    colonne d'identifiant si elle diffère de ``'id'`` (voir ``table.id_col``,
    renvoyé par la même fonction). Retourne ``None`` si ``basins`` est
    ``None`` (repli sur des zones tampon autour des points, voir
    ``_point_buffers``).
    """
    import geopandas as gpd

    if basins is None:
        return None
    if not isinstance(basins, gpd.GeoDataFrame):
        raise TypeError(
            "basins doit être un GeoDataFrame de polygones de sous-bassins "
            "(voir glofas_basins.select_basins_in_zone)."
        )
    id_col = basin_id_col or ("id" if "id" in basins.columns else None)
    if id_col is None:
        raise ValueError(
            "Impossible de déterminer la colonne d'identifiant des sous-bassins -- "
            "précisez basin_id_col (ex. table.id_col renvoyé par select_basins_in_zone)."
        )
    gdf = basins[[id_col, "geometry"]].rename(columns={id_col: "id"}).copy()
    gdf["id"] = gdf["id"].astype(str)
    return _ensure_wgs84(gdf)


def _point_buffers(points_meta: pd.DataFrame, radius_km: float):
    """Zones circulaires (buffer) autour de chaque point -- repli utilisé
    quand aucune géométrie de sous-bassin n'est fournie (``basins=None``).
    Reprojection dans un UTM estimé (``estimate_utm_crs``) pour un rayon
    métrique correct, comme ``glofas_basins.resolve_zone(buffer_km=...)``.
    """
    import geopandas as gpd

    missing = {"id", "lon_pixel", "lat_pixel"} - set(points_meta.columns)
    if missing:
        raise ValueError(
            f"Colonnes manquantes dans points_meta : {sorted(missing)} -- utilisez le fichier "
            "'*_points.csv' produit par extract_glofas_at_points."
        )
    gdf = gpd.GeoDataFrame(
        {"id": points_meta["id"].astype(str)},
        geometry=gpd.points_from_xy(points_meta["lon_pixel"], points_meta["lat_pixel"]),
        crs="EPSG:4326",
    )
    utm = gdf.estimate_utm_crs()
    buffered = gdf.to_crs(utm)
    buffered["geometry"] = buffered.buffer(radius_km * 1000)
    return buffered.to_crs("EPSG:4326")


def _north_arrow(ax, *, x: float = 0.94, y: float = 0.90, size: float = 0.06):
    """Petite flèche du nord (le nord est toujours en haut de la carte --
    pas de rotation, les projections utilisées ici n'en ont pas besoin)."""
    ax.annotate(
        "N", xy=(x, y), xytext=(x, y - size), xycoords="axes fraction",
        textcoords="axes fraction", ha="center", va="center",
        fontsize=13, fontweight="bold", color="#1a1a19",
        arrowprops=dict(arrowstyle="-|>", color="#1a1a19", lw=1.6, shrinkA=0, shrinkB=0),
        zorder=10,
    )


def _scale_bar(ax, *, x_frac: float = 0.05, y_frac: float = 0.05, length_km: float | None = None):
    """Barre d'échelle (segments noir/blanc alternés + libellé). Longueur
    arrondie automatiquement (environ un quart de la largeur affichée) si
    non précisée. Conversion degrés<->km approximative (sphère, latitude
    moyenne de l'emprise affichée) : indicative, pas une mesure de
    précision -- suffisant pour une carte de risque, pas pour du SIG.
    """
    import math

    from matplotlib.patches import Rectangle

    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    mean_lat = (ymin + ymax) / 2
    km_per_degree_lon = 111.32 * math.cos(math.radians(mean_lat)) or 111.32
    width_km = (xmax - xmin) * km_per_degree_lon

    if length_km is None:
        target = max(width_km / 4, 1.0)
        magnitude = 10 ** math.floor(math.log10(target))
        length_km = target
        for mult in (1, 2, 5, 10):
            candidate = mult * magnitude
            if candidate >= target:
                length_km = candidate
                break

    length_deg = length_km / km_per_degree_lon
    x0 = xmin + x_frac * (xmax - xmin)
    y0 = ymin + y_frac * (ymax - ymin)
    bar_height = 0.012 * (ymax - ymin)
    n_segments = 4
    seg_len = length_deg / n_segments
    for i in range(n_segments):
        color = "#1a1a19" if i % 2 == 0 else "#ffffff"
        ax.add_patch(Rectangle(
            (x0 + i * seg_len, y0), seg_len, bar_height,
            facecolor=color, edgecolor="#1a1a19", linewidth=0.6, zorder=10,
        ))
    ax.text(x0, y0 + bar_height * 1.9, f"{length_km:g} km", fontsize=8, ha="left", va="bottom", zorder=10)


def build_static_risk_map(
    risk_table: str | Path | pd.DataFrame,
    points_meta: str | Path | pd.DataFrame,
    output_path: str | Path | None = None,
    *,
    issue_date: str | None = None,
    leadtime_hours: int | None = None,
    basins=None,
    basin_id_col: str | None = None,
    countries_path: str | Path = "static/afrique.gpkg",
    rivers_path: str | Path | None = None,
    zone=None,
    point_radius_km: float = 15.0,
    title: str | None = None,
    ax=None,
    figsize: tuple = (10, 8),
    dpi: int = 150,
):
    """Carte de risque « statique » (PNG imprimable), dans l'esprit des
    bulletins CILSS/AGRHYMET : zones colorées par catégorie de risque
    (plutôt que des marqueurs ponctuels comme ``build_risk_map``), limites
    administratives, flèche du nord, barre d'échelle et légende à swatches.

    Deux modes de rendu des zones, selon ``basins`` :

    - ``basins`` fourni (GeoDataFrame de polygones de sous-bassins -- p. ex.
      la variable ``selection`` renvoyée par
      ``glofas_basins.select_basins_in_zone``, avec
      ``basin_id_col=table.id_col``) : chaque sous-bassin est colorié selon
      sa catégorie de risque -- rendu le plus proche des cartes officielles.
    - ``basins=None`` (défaut) : repli sur un disque de ``point_radius_km``
      km autour de chaque point (maille retenue) -- fonctionne sans
      géométrie de sous-bassin, mais ne représente qu'une zone d'influence
      approximative, pas le contour réel du bassin (avertissement journalisé).

    ``rivers_path`` (optionnel) : couche vectorielle du réseau hydrographique
    à afficher en fond (non fournie avec l'atelier -- à ajouter si
    disponible). ``zone`` (optionnel, voir ``build_risk_map``) : contour
    pointillé + cadrage de la carte, comme pour la carte interactive.

    **Limite importante, à garder en tête** : contrairement aux cartes
    officielles GloFAS/CILSS (qui classent chaque pixel du réseau
    hydrographique sur tout le domaine), cette carte ne colore que les
    sous-bassins/points effectivement sélectionnés et extraits par la
    chaîne -- le reste du fond de carte apparaît en gris clair uniforme, pas
    en vert « normal » : on ne peint jamais une zone comme « normale » sans
    donnée pour l'affirmer.

    Mêmes règles de sélection (``issue_date``/``leadtime_hours``) que
    ``build_risk_map``. Retourne ``(fig, ax)`` si ``ax`` n'est pas fourni ;
    sinon dessine directement sur ``ax`` (utilisé par
    ``generate_static_risk_map_grid``) et retourne ``ax`` (``output_path``
    est alors ignoré -- la figure appartient à l'appelant).
    """
    import matplotlib.pyplot as plt
    import geopandas as gpd

    from glofas_risk import RISK_LABELS, RISK_ORDER

    risk = _load_table(risk_table)
    meta = _load_table(points_meta).drop_duplicates("id").copy()
    if risk.empty:
        raise ValueError("Table de risque vide.")
    meta["id"] = meta["id"].astype(str)

    available_issues = sorted(risk["issue_date"].astype(str).unique())
    if issue_date is None:
        issue_date = available_issues[-1]
        if len(available_issues) > 1:
            LOG.info("issue_date non précisé : utilisation de la plus récente (%s)", issue_date)
    subset = risk[risk["issue_date"].astype(str) == str(issue_date)].copy()
    if subset.empty:
        raise ValueError(f"Aucune donnée pour issue_date={issue_date!r}. Dates disponibles : {available_issues}")
    subset["id"] = subset["id"].astype(str)

    available_leadtimes = sorted(int(h) for h in subset["leadtime_hours"].unique())
    if leadtime_hours is None:
        if len(available_leadtimes) > 1:
            raise ValueError(
                f"Plusieurs échéances disponibles pour {issue_date} ({available_leadtimes} h) -- "
                "précisez leadtime_hours."
            )
        leadtime_hours = available_leadtimes[0]
    subset = subset[subset["leadtime_hours"] == leadtime_hours]
    if subset.empty:
        raise ValueError(f"Aucune donnée pour leadtime_hours={leadtime_hours} (disponibles : {available_leadtimes})")

    zones_gdf = _basin_geometries(basins, basin_id_col)
    if zones_gdf is None:
        LOG.warning(
            "Pas de géométrie de sous-bassin fournie (basins=None) -- zones affichées comme des disques "
            "de %.0f km de rayon autour de chaque point, pas le contour réel du bassin.",
            point_radius_km,
        )
        zones_gdf = _point_buffers(meta.merge(subset[["id"]].drop_duplicates(), on="id"), point_radius_km)

    data = zones_gdf.merge(subset[["id", "risque"]], on="id", how="inner")
    absent = sorted(set(zones_gdf["id"]) - set(subset["id"]))
    if absent:
        LOG.warning("Sous-bassins/points sans classification de risque pour cette échéance, ignorés : %s", absent)
    if data.empty:
        raise ValueError("Aucune zone à afficher (aucune correspondance entre basins/points et la table de risque).")

    own_fig = ax is None
    if own_fig:
        fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    else:
        fig = ax.figure
        if output_path is not None:
            LOG.warning("output_path ignoré quand ax est fourni (la figure appartient à l'appelant).")

    countries = _country_basemap(countries_path)
    countries.plot(ax=ax, facecolor="#f2f1ee", edgecolor="#4d4d4d", linewidth=0.6, zorder=1)

    if rivers_path is not None:
        rivers = _ensure_wgs84(gpd.read_file(rivers_path))
        rivers.plot(ax=ax, color="#7fb0e0", linewidth=0.5, alpha=0.85, zorder=2)

    for level in RISK_ORDER:
        level_data = data[data["risque"] == level]
        if level_data.empty:
            continue
        level_data.plot(ax=ax, facecolor=RISK_COLORS[level], edgecolor="none", zorder=3)

    zone_geom = _zone_geometry(zone)
    if zone_geom is not None:
        gpd.GeoSeries([zone_geom], crs="EPSG:4326").boundary.plot(
            ax=ax, color="#222222", linewidth=1.2, linestyle=(0, (5, 4)), zorder=4,
        )
        minx, miny, maxx, maxy = zone_geom.bounds
    else:
        minx, miny, maxx, maxy = data.total_bounds
    pad_x = (maxx - minx) * 0.12 or 0.5
    pad_y = (maxy - miny) * 0.12 or 0.5
    ax.set_xlim(minx - pad_x, maxx + pad_x)
    ax.set_ylim(miny - pad_y, maxy + pad_y)

    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_color("#999999")

    day = int(leadtime_hours) // 24
    default_title = f"Émission {issue_date} — échéance J+{day}"
    ax.set_title(title if title is not None else default_title, fontsize=11, fontweight="bold", pad=8)

    _north_arrow(ax)
    _scale_bar(ax)

    if own_fig:
        legend_handles = [
            plt.Rectangle((0, 0), 1, 1, facecolor=RISK_COLORS[level], edgecolor="none")
            for level in RISK_ORDER
        ]
        fig.legend(
            legend_handles, [RISK_LABELS[level] for level in RISK_ORDER],
            loc="lower center", ncol=len(RISK_ORDER), frameon=False,
            bbox_to_anchor=(0.5, -0.02), fontsize=9,
            title="Niveau de risque", title_fontsize=9,
        )
        fig.tight_layout(rect=(0, 0.05, 1, 1))

        if output_path is not None:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(output_path, bbox_inches="tight")
            LOG.info("Carte statique enregistrée : %s", output_path)
        return fig, ax

    return ax


def build_max_severity_map(
    risk_table: str | Path | pd.DataFrame,
    points_meta: str | Path | pd.DataFrame,
    output_path: str | Path | None = None,
    *,
    issue_date: str | None = None,
    leadtime_hours: Sequence[int] | None = None,
    max_day: int | None = None,
    basins=None,
    basin_id_col: str | None = None,
    countries_path: str | Path = "static/afrique.gpkg",
    rivers_path: str | Path | None = None,
    zone=None,
    point_radius_km: float = 15.0,
    title: str | None = None,
    figsize: tuple = (10, 8),
    dpi: int = 150,
):
    """Carte de risque « maximal » sur (tout ou partie de) la période de
    prévision : pour chaque sous-bassin/point, retient la catégorie de
    risque la plus sévère parmi les échéances considérées -- même principe
    que les cartes CILSS "Maximum forecast flood hazard severity from ...
    to ...".

    ``leadtime_hours`` restreint explicitement les échéances retenues
    (toutes les échéances disponibles pour ``issue_date`` par défaut) ;
    ``max_day`` est un raccourci pour ne garder que J+1 à J+``max_day``.
    Au plus un des deux à la fois. Le reste des paramètres est identique à
    ``build_static_risk_map`` (mêmes limites, notamment sur ``basins=None``).
    """
    from glofas_risk import RISK_ORDER

    if leadtime_hours is not None and max_day is not None:
        raise ValueError("Précisez leadtime_hours OU max_day, pas les deux.")

    risk = _load_table(risk_table)
    if risk.empty:
        raise ValueError("Table de risque vide.")
    risk = risk.copy()
    risk["id"] = risk["id"].astype(str)

    available_issues = sorted(risk["issue_date"].astype(str).unique())
    if issue_date is None:
        issue_date = available_issues[-1]
        if len(available_issues) > 1:
            LOG.info("issue_date non précisé : utilisation de la plus récente (%s)", issue_date)
    subset = risk[risk["issue_date"].astype(str) == str(issue_date)].copy()
    if subset.empty:
        raise ValueError(f"Aucune donnée pour issue_date={issue_date!r}. Dates disponibles : {available_issues}")

    all_leadtimes = sorted(int(h) for h in subset["leadtime_hours"].unique())
    if max_day is not None:
        keep = {h for h in all_leadtimes if h <= max_day * 24}
        if not keep:
            raise ValueError(f"Aucune échéance <= J+{max_day} (disponibles : {all_leadtimes} h).")
    elif leadtime_hours is not None:
        keep = {int(h) for h in leadtime_hours}
        unknown = keep - set(all_leadtimes)
        if unknown:
            raise ValueError(f"Échéance(s) inconnue(s) : {sorted(unknown)} (disponibles : {all_leadtimes})")
    else:
        keep = set(all_leadtimes)
    subset = subset[subset["leadtime_hours"].isin(keep)].copy()

    used_leadtimes = sorted(int(h) for h in subset["leadtime_hours"].unique())
    order_rank = {lvl: i for i, lvl in enumerate(RISK_ORDER)}
    subset["_rang"] = subset["risque"].map(order_rank).fillna(0)
    worst = subset.loc[subset.groupby("id")["_rang"].idxmax(), ["id", "risque"]].reset_index(drop=True)

    day_start, day_end = used_leadtimes[0] // 24, used_leadtimes[-1] // 24
    issue_ts = pd.Timestamp(issue_date)
    period_start = issue_ts + pd.Timedelta(days=day_start)
    period_end = issue_ts + pd.Timedelta(days=day_end)
    default_title = (
        f"Sévérité maximale du risque d'inondation prévu\n"
        f"du {_format_date_fr(period_start)} au {_format_date_fr(period_end)} "
        f"(émission {issue_date}, J+{day_start} à J+{day_end})"
    )

    worst_as_risk = worst.assign(issue_date=issue_date, leadtime_hours=used_leadtimes[0])

    return build_static_risk_map(
        worst_as_risk, points_meta, output_path,
        issue_date=issue_date, leadtime_hours=used_leadtimes[0],
        basins=basins, basin_id_col=basin_id_col,
        countries_path=countries_path, rivers_path=rivers_path, zone=zone,
        point_radius_km=point_radius_km,
        title=title if title is not None else default_title,
        figsize=figsize, dpi=dpi,
    )


def generate_daily_static_risk_maps(
    risk_table: str | Path | pd.DataFrame,
    points_meta: str | Path | pd.DataFrame,
    output_dir: str | Path,
    *,
    issue_date: str | None = None,
    basins=None,
    basin_id_col: str | None = None,
    countries_path: str | Path = "static/afrique.gpkg",
    rivers_path: str | Path | None = None,
    zone=None,
    point_radius_km: float = 15.0,
    figsize: tuple = (8, 6.5),
    dpi: int = 150,
) -> list[Path]:
    """Équivalent statique (PNG imprimable) de ``generate_daily_risk_maps`` :
    une carte par échéance journalière disponible, pour une date d'émission
    donnée (la plus récente par défaut). Fichiers nommés
    ``carte_risque_<issue_date>_Jplus<NN>.png`` dans ``output_dir``.
    """
    import matplotlib.pyplot as plt

    risk = _load_table(risk_table)
    if risk.empty:
        raise ValueError("Table de risque vide.")

    available_issues = sorted(risk["issue_date"].astype(str).unique())
    if issue_date is None:
        issue_date = available_issues[-1]
        if len(available_issues) > 1:
            LOG.info("issue_date non précisé : utilisation de la plus récente (%s)", issue_date)
    subset = risk[risk["issue_date"].astype(str) == str(issue_date)]
    if subset.empty:
        raise ValueError(f"Aucune donnée pour issue_date={issue_date!r}. Dates disponibles : {available_issues}")

    leadtimes = sorted(int(h) for h in subset["leadtime_hours"].unique())
    if not leadtimes:
        raise ValueError(f"Aucune échéance disponible pour issue_date={issue_date!r}.")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for hours in leadtimes:
        day = hours // 24
        out_file = output_path / f"carte_risque_{issue_date}_Jplus{day:02d}.png"
        fig, _ax = build_static_risk_map(
            risk, points_meta, out_file,
            issue_date=issue_date, leadtime_hours=hours,
            basins=basins, basin_id_col=basin_id_col,
            countries_path=countries_path, rivers_path=rivers_path, zone=zone,
            point_radius_km=point_radius_km, figsize=figsize, dpi=dpi,
        )
        plt.close(fig)
        written.append(out_file)

    LOG.info(
        "Écrit %d carte(s) statique(s) journalières dans %s (émission %s, échéances %s h).",
        len(written), output_path, issue_date, leadtimes,
    )
    return written


def generate_static_risk_map_grid(
    risk_table: str | Path | pd.DataFrame,
    points_meta: str | Path | pd.DataFrame,
    output_path: str | Path,
    *,
    issue_date: str | None = None,
    ncols: int = 5,
    basins=None,
    basin_id_col: str | None = None,
    countries_path: str | Path = "static/afrique.gpkg",
    rivers_path: str | Path | None = None,
    zone=None,
    point_radius_km: float = 15.0,
    panel_size: tuple = (3.4, 3.6),
    dpi: int = 150,
):
    """Planche unique regroupant toutes les cartes journalières (une
    vignette par échéance disponible, ``ncols`` colonnes), avec titre par
    vignette (date calendaire, pas juste « J+N ») et légende partagée en
    bas de figure -- même présentation que les planches CILSS/AGRHYMET (une
    grille de vignettes pour ~10 jours de prévision).

    Retourne la figure matplotlib ; toujours enregistrée dans
    ``output_path`` (contrairement à ``build_static_risk_map``, il n'y a pas
    ici de sens à ne pas l'enregistrer puisque la planche assemble
    plusieurs sous-cartes).
    """
    import math

    import matplotlib.pyplot as plt

    from glofas_risk import RISK_LABELS, RISK_ORDER

    risk = _load_table(risk_table)
    if risk.empty:
        raise ValueError("Table de risque vide.")

    available_issues = sorted(risk["issue_date"].astype(str).unique())
    if issue_date is None:
        issue_date = available_issues[-1]
        if len(available_issues) > 1:
            LOG.info("issue_date non précisé : utilisation de la plus récente (%s)", issue_date)
    subset = risk[risk["issue_date"].astype(str) == str(issue_date)]
    if subset.empty:
        raise ValueError(f"Aucune donnée pour issue_date={issue_date!r}. Dates disponibles : {available_issues}")

    leadtimes = sorted(int(h) for h in subset["leadtime_hours"].unique())
    if not leadtimes:
        raise ValueError(f"Aucune échéance disponible pour issue_date={issue_date!r}.")

    n = len(leadtimes)
    ncols = max(1, min(ncols, n))
    nrows = math.ceil(n / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(panel_size[0] * ncols, panel_size[1] * nrows), dpi=dpi)
    axes_flat = list(axes.flatten()) if hasattr(axes, "flatten") else [axes]

    issue_ts = pd.Timestamp(issue_date)
    for ax, hours in zip(axes_flat, leadtimes):
        day = hours // 24
        panel_date = issue_ts + pd.Timedelta(days=day)
        build_static_risk_map(
            risk, points_meta, None, ax=ax,
            issue_date=issue_date, leadtime_hours=hours,
            basins=basins, basin_id_col=basin_id_col,
            countries_path=countries_path, rivers_path=rivers_path, zone=zone,
            point_radius_km=point_radius_km,
            title=_format_date_fr(panel_date),
        )
    for ax in axes_flat[n:]:
        ax.axis("off")

    legend_handles = [
        plt.Rectangle((0, 0), 1, 1, facecolor=RISK_COLORS[level], edgecolor="none")
        for level in RISK_ORDER
    ]
    fig.legend(
        legend_handles, [RISK_LABELS[level] for level in RISK_ORDER],
        loc="lower center", ncol=len(RISK_ORDER), frameon=False,
        bbox_to_anchor=(0.5, 0.0), fontsize=10,
        title="Niveau de risque", title_fontsize=10,
    )
    fig.suptitle(f"Prévision de risque d'inondation — émission {issue_date}", fontsize=13, fontweight="bold", y=1.02)
    fig.tight_layout(rect=(0, 0.06, 1, 0.98))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    LOG.info("Planche de cartes journalières enregistrée : %s (%d échéance(s))", output_path, n)

    return fig


def interactive_risk_explorer(
    risk_table: str | Path | pd.DataFrame,
    points_meta: str | Path | pd.DataFrame,
    *,
    default_output: str | Path = "resultats/carte_risque_perso.html",
):
    """Affiche, dans un notebook Jupyter, un sélecteur de date d'émission et
    d'échéance pour explorer la carte de risque, avec enregistrement de la
    carte affichée.

    Nécessite ``ipywidgets`` (sans effet en dehors d'un notebook Jupyter --
    utilisez directement ``build_risk_map(..., issue_date=..., leadtime_hours=...)``
    dans un script).
    """
    try:
        import ipywidgets as widgets
    except ImportError as exc:
        raise RuntimeError(
            "ipywidgets est requis pour l'explorateur interactif : "
            "pip install ipywidgets (ou conda install -c conda-forge ipywidgets)"
        ) from exc
    from IPython.display import display

    risk = _load_table(risk_table)
    meta = _load_table(points_meta)
    if risk.empty:
        raise ValueError("Table de risque vide.")

    issue_dates = sorted(risk["issue_date"].astype(str).unique())

    def _leadtimes_for(issue: str) -> list[int]:
        return sorted(risk.loc[risk["issue_date"].astype(str) == issue, "leadtime_hours"].unique())

    issue_picker = widgets.Dropdown(options=issue_dates, value=issue_dates[-1], description="Émission")
    leadtimes0 = _leadtimes_for(issue_picker.value)
    leadtime_picker = widgets.SelectionSlider(
        options=[(f"J+{h // 24}", h) for h in leadtimes0],
        value=leadtimes0[0],
        description="Échéance",
        continuous_update=False,
        layout=widgets.Layout(width="420px"),
    )
    filename_box = widgets.Text(value=str(default_output), description="Fichier", layout=widgets.Layout(width="420px"))
    save_button = widgets.Button(description="Enregistrer la carte", icon="save", button_style="success")
    save_status = widgets.Output()
    map_output = widgets.Output()

    def render(*_change):
        with map_output:
            map_output.clear_output(wait=True)
            try:
                fmap = build_risk_map(risk, meta, issue_date=issue_picker.value, leadtime_hours=leadtime_picker.value)
            except ValueError as exc:
                print(exc)
                return
            display(fmap)

    def on_issue_change(_change):
        new_leadtimes = _leadtimes_for(issue_picker.value)
        leadtime_picker.options = [(f"J+{h // 24}", h) for h in new_leadtimes]
        leadtime_picker.value = new_leadtimes[0]
        render()

    def on_save_clicked(_button):
        with save_status:
            save_status.clear_output(wait=True)
            try:
                fmap = build_risk_map(risk, meta, issue_date=issue_picker.value, leadtime_hours=leadtime_picker.value)
                output_path = Path(filename_box.value)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                fmap.save(str(output_path))
                print(f"Carte enregistrée : {output_path}")
            except ValueError as exc:
                print(exc)

    issue_picker.observe(on_issue_change, names="value")
    leadtime_picker.observe(render, names="value")
    save_button.on_click(on_save_clicked)

    controls = widgets.VBox(
        [
            widgets.HBox([issue_picker, leadtime_picker]),
            widgets.HBox([filename_box, save_button]),
            save_status,
        ]
    )
    display(controls, map_output)
    render()


# --------------------------------------------------------------------------- #
# Rapport HTML combiné
# --------------------------------------------------------------------------- #


def build_report(
    series: str | Path | pd.DataFrame,
    points_meta: str | Path | pd.DataFrame,
    output_prefix: str | Path,
    *,
    title: str = "Extraction GloFAS — rapport",
) -> dict[str, Path]:
    """Génère la carte, le graphique et une page HTML autonome qui les combine
    (via des ``<iframe>`` vers les deux fichiers, donc à garder dans le même
    dossier).

    Retourne les chemins des trois fichiers écrits.
    """
    output_prefix = Path(output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    series_df = _load_table(series)
    meta_df = _points_status(_load_table(points_meta))

    map_path = output_prefix.with_name(output_prefix.name + "_carte.html")
    series_path = output_prefix.with_name(output_prefix.name + "_series.html")
    report_path = output_prefix.with_name(output_prefix.name + "_rapport.html")

    build_points_map(meta_df, map_path)
    build_timeseries_figure(series_df, series_path)

    counts = meta_df["statut"].value_counts()
    summary_badges = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px;font-size:13px;">'
        f'<span style="width:10px;height:10px;border-radius:50%;background:{STATUS_COLORS[s]};display:inline-block;"></span>'
        f"{STATUS_LABELS[s]} : {int(counts.get(s, 0))}</span>"
        for s in STATUS_ORDER
    )

    rows_html = "".join(
        "<tr>"
        f"<td>{row.id}</td>"
        f"<td>{row.lon_input:.4f}</td><td>{row.lat_input:.4f}</td>"
        f"<td>{row.lon_pixel:.4f}</td><td>{row.lat_pixel:.4f}</td>"
        f"<td>{row.distance_km:.2f}</td>"
        f'<td><span style="color:{STATUS_COLORS[row.statut]};font-weight:600;">{STATUS_LABELS[row.statut]}</span></td>'
        "</tr>"
        for row in meta_df.itertuples()
    )

    html = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    margin: 0; padding: 32px; background: #faf9f7; color: #1a1a19;
  }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .subtitle {{ color: #666; font-size: 13px; margin-bottom: 24px; }}
  h2 {{ font-size: 16px; margin: 32px 0 12px; }}
  .card {{
    background: #fff; border: 1px solid #e5e3df; border-radius: 10px;
    overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}
  iframe {{ width: 100%; border: 0; display: block; }}
  #map-frame {{ height: 560px; }}
  #series-frame {{ height: 620px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ border-bottom: 1px solid #e5e3df; padding: 8px 12px; text-align: left; }}
  th {{ background: #f0efec; font-weight: 600; }}
  tbody tr:hover {{ background: #f7f6f3; }}
  .summary {{ margin-bottom: 8px; }}
</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="subtitle">{len(meta_df)} point(s) — {series_df["id"].nunique() if "id" in series_df.columns else "?"} série(s) temporelle(s)</div>

  <h2>Carte des points</h2>
  <div class="summary">{summary_badges}</div>
  <div class="card"><iframe id="map-frame" src="{map_path.name}"></iframe></div>

  <h2>Résumé du recalage spatial</h2>
  <div class="card">
    <table>
      <thead><tr>
        <th>ID</th><th>Lon. saisie</th><th>Lat. saisie</th>
        <th>Lon. retenue</th><th>Lat. retenue</th><th>Distance (km)</th><th>Statut</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>

  <h2>Séries temporelles</h2>
  <div class="card"><iframe id="series-frame" src="{series_path.name}"></iframe></div>
</body>
</html>
"""
    report_path.write_text(html, encoding="utf-8")
    LOG.info("Rapport enregistré : %s (+ %s, %s)", report_path, map_path.name, series_path.name)

    return {"map": map_path, "series": series_path, "report": report_path}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Génère une carte interactive et un rapport HTML à partir des sorties de glofas_extract.py"
    )
    parser.add_argument("--series", required=True, help="CSV des séries temporelles (*_series.csv)")
    parser.add_argument("--points", required=True, help="CSV du résumé par point (*_points.csv)")
    parser.add_argument("--output", required=True, help="Préfixe des fichiers de sortie (ex: resultats/extraction)")
    parser.add_argument(
        "--ids",
        nargs="+",
        default=None,
        help="Limite le graphique temporel à ces stations (écrit en plus <output>_series_filtre.html)",
    )
    parser.add_argument("--start", default=None, help="Début de période pour le graphique filtré (YYYY, YYYY-MM ou YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="Fin de période pour le graphique filtré")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    try:
        build_report(args.series, args.points, args.output)
        if args.ids or args.start or args.end:
            from glofas_extract import _parse_period_bound

            filtered_path = Path(args.output).with_name(Path(args.output).name + "_series_filtre.html")
            build_timeseries_figure(
                args.series,
                filtered_path,
                ids=args.ids,
                start=_parse_period_bound(args.start, end=False) if args.start else None,
                end=_parse_period_bound(args.end, end=True) if args.end else None,
            )
    except Exception as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
