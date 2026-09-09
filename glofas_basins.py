"""Sélection automatique de sous-bassins par zone d'étude (pays ou zone
personnalisée), pour générer le CSV de points attendu par le reste de la
chaîne (``glofas_extract``/``glofas_forecast``/``glofas_risk``).

Deux couches statiques (livrées par l'utilisateur, dossier ``static/``) :

- ``afrique.gpkg`` : limites administratives des pays d'Afrique, colonne
  ``GMI_CNTRY`` (code ISO3) -- sert à résoudre une zone d'étude à partir d'un
  simple code pays.
- ``hybas_af_lev05_with_outlets.gpkg`` (ou son équivalent CSV plat
  ``hybas_af_lev05_outlet_coordinates.csv``) : sous-bassins HydroBASINS
  niveau 5 (Afrique), avec les coordonnées de leur exutoire dans les colonnes
  ``OUTLET_LONGITUDE``/``OUTLET_LATITUDE``.

Principe : l'utilisateur définit une zone d'étude, soit par un code ISO3
(``iso3="CMR"``), soit en fournissant son propre shapefile/GeoPackage
(``boundary_path=...``). On sélectionne les sous-bassins qui tombent dans
cette zone (``method`` -- voir plus bas), puis on exporte les coordonnées de
leurs exutoires au format ``ID, LONG, LAT`` attendu par
``glofas_extract.read_points_csv`` : le reste de la chaîne (téléchargement,
extraction historique et prévision, seuils, classification, cartes) est
ensuite utilisé sans aucune modification.

Trois méthodes de sélection (``method``) :

- ``"outlet"`` (défaut) : le point d'exutoire du sous-bassin doit tomber
  dans la zone. Ne nécessite pas la géométrie des polygones -- fonctionne
  aussi bien avec le GeoPackage complet qu'avec le CSV plat des seules
  coordonnées. C'est la méthode la plus cohérente avec le reste de la chaîne
  (l'extraction GloFAS se fait justement au pixel le plus proche de ce
  point).
- ``"intersects"`` : le polygone du sous-bassin touche la zone (au moins
  partiellement) -- plus permissif, capture aussi les bassins transfrontaliers
  dont une partie seulement est dans la zone. Nécessite la géométrie des
  polygones (GeoPackage/shapefile, pas le CSV plat).
- ``"within"`` : le polygone du sous-bassin est entièrement contenu dans la
  zone -- plus strict. Nécessite également la géométrie des polygones.

Utilisation typique ::

    from glofas_basins import build_zone_points

    zone, points = build_zone_points(
        iso3="CMR",
        basins_path="static/hybas_af_lev05_with_outlets.gpkg",
        method="outlet",
        output="resultats/points_cmr.csv",
    )

``points`` est alors passé tel quel à ``glofas_download``/``glofas_extract``
(colonnes ``ID``, ``LONG``, ``LAT``), exactement comme un CSV de points saisi
à la main.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pandas as pd

LOG = logging.getLogger("glofas_basins")

DEFAULT_COUNTRIES_GPKG = "static/afrique.gpkg"
DEFAULT_COUNTRY_COL = "GMI_CNTRY"
DEFAULT_BASINS_SOURCE = "static/hybas_af_lev05_with_outlets.gpkg"

METHODS = ("outlet", "intersects", "within")

# Colonnes candidates (ordre de préférence), comparaison insensible à la casse.
_ID_CANDIDATES = ("HYBAS_ID_LEV05", "HYBAS_ID", "HYBAS_ID_05", "hybas_id", "id", "ID")
_LON_CANDIDATES = ("OUTLET_LONGITUDE", "outlet_longitude", "LONG", "lon", "longitude")
_LAT_CANDIDATES = ("OUTLET_LATITUDE", "outlet_latitude", "LAT", "lat", "latitude")


def _require_geopandas():
    try:
        import geopandas as gpd  # noqa: F401
    except ImportError as exc:  # pragma: no cover - dépendance optionnelle
        raise ImportError(
            "geopandas est requis par glofas_basins.py : "
            "pip install geopandas pyogrio shapely --break-system-packages "
            "(ou installez l'environnement conda mis à jour, voir environment.yml)."
        ) from exc
    return gpd


def _pick_column(columns: Sequence[str], candidates: Sequence[str], role: str) -> str:
    columns = list(columns)
    lower = {str(c).strip().lower(): c for c in columns}
    for cand in candidates:
        if cand in columns:
            return cand
        if cand.lower() in lower:
            return lower[cand.lower()]
    raise ValueError(
        f"Impossible de trouver une colonne pour '{role}' parmi {columns} "
        f"(essayé : {list(candidates)}). Précisez le paramètre correspondant."
    )


# --------------------------------------------------------------------------- #
# Zone d'étude
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Zone:
    """Zone d'étude résolue : une géométrie unique (WGS84 / EPSG:4326)."""

    geometry: object  # shapely Polygon/MultiPolygon
    label: str
    source: str


def load_countries(countries_path: str | Path = DEFAULT_COUNTRIES_GPKG, *, country_col: str = DEFAULT_COUNTRY_COL):
    """Charge la couche des pays et vérifie la présence de ``country_col``."""
    gpd = _require_geopandas()
    path = Path(countries_path)
    if not path.is_file():
        raise FileNotFoundError(f"Fichier des limites administratives introuvable : {path}")
    gdf = gpd.read_file(path)
    if country_col not in gdf.columns:
        raise ValueError(
            f"Colonne '{country_col}' absente de {path} (colonnes disponibles : {list(gdf.columns)})."
        )
    return _ensure_wgs84(gdf, str(path))


def _ensure_wgs84(gdf, source_label: str):
    if gdf.crs is None:
        LOG.warning("Pas de CRS déclaré dans %s -- on suppose EPSG:4326 (WGS84).", source_label)
        return gdf.set_crs("EPSG:4326")
    if gdf.crs.to_epsg() != 4326:
        LOG.info("Reprojection de %s de %s vers EPSG:4326.", source_label, gdf.crs)
        return gdf.to_crs("EPSG:4326")
    return gdf


def _buffer_geometry_km(geometry, buffer_km: float):
    """Tampon en kilomètres, via reprojection dans un CRS métrique (UTM estimé)."""
    gpd = _require_geopandas()
    series = gpd.GeoSeries([geometry], crs="EPSG:4326")
    utm = series.estimate_utm_crs()
    buffered = series.to_crs(utm).buffer(buffer_km * 1000.0).to_crs("EPSG:4326")
    return buffered.iloc[0]


def resolve_zone(
    *,
    iso3: str | None = None,
    boundary_path: str | Path | None = None,
    countries_path: str | Path = DEFAULT_COUNTRIES_GPKG,
    country_col: str = DEFAULT_COUNTRY_COL,
    buffer_km: float = 0.0,
) -> Zone:
    """Résout la zone d'étude à partir d'un code ISO3 OU d'un fichier limite.

    Précisez exactement l'un des deux :

    - ``iso3`` : code pays à 3 lettres (ex. ``"CMR"``), recherché dans la
      colonne ``country_col`` de ``countries_path`` (``afrique.gpkg`` /
      ``GMI_CNTRY`` par défaut).
    - ``boundary_path`` : chemin vers un shapefile/GeoPackage/GeoJSON
      contenant la zone -- toutes ses entités sont fusionnées en une seule
      géométrie (peu importe qu'il y ait une ou plusieurs entités).

    ``buffer_km`` (optionnel) ajoute une marge autour de la zone (utile pour
    inclure les sous-bassins juste à l'extérieur d'une frontière, par
    exemple).
    """
    gpd = _require_geopandas()
    if (iso3 is None) == (boundary_path is None):
        raise ValueError("Précisez exactement l'un de 'iso3' ou 'boundary_path' (pas les deux, pas aucun).")

    if iso3 is not None:
        code = str(iso3).strip().upper()
        countries = load_countries(countries_path, country_col=country_col)
        match = countries[countries[country_col].astype(str).str.upper() == code]
        if match.empty:
            valid = sorted(countries[country_col].astype(str).unique())
            raise ValueError(
                f"Code ISO3 {code!r} introuvable dans {countries_path} (colonne {country_col!r}). "
                f"Codes disponibles : {valid}"
            )
        geometry = match.union_all() if hasattr(match, "union_all") else match.unary_union
        label = code
        source = f"{countries_path} ({country_col}={code})"
    else:
        path = Path(boundary_path)
        if not path.is_file():
            raise FileNotFoundError(f"Fichier de zone introuvable : {path}")
        gdf = gpd.read_file(path)
        if gdf.empty:
            raise ValueError(f"Le fichier de zone est vide : {path}")
        gdf = _ensure_wgs84(gdf, str(path))
        geometry = gdf.union_all() if hasattr(gdf, "union_all") else gdf.unary_union
        label = path.stem
        source = str(path)

    if buffer_km:
        if buffer_km < 0:
            raise ValueError("buffer_km doit être positif ou nul.")
        geometry = _buffer_geometry_km(geometry, buffer_km)

    return Zone(geometry=geometry, label=label, source=source)


# --------------------------------------------------------------------------- #
# Sous-bassins
# --------------------------------------------------------------------------- #


@dataclass
class BasinsTable:
    """Couche des sous-bassins chargée, avec les noms de colonnes résolus."""

    gdf: object  # GeoDataFrame, CRS EPSG:4326, colonne 'outlet_geometry' toujours présente
    id_col: str
    lon_col: str
    lat_col: str
    has_polygon: bool
    source: str


def load_basins(
    basins_path: str | Path = DEFAULT_BASINS_SOURCE,
    *,
    id_col: str | None = None,
    lon_col: str | None = None,
    lat_col: str | None = None,
    layer: str | None = None,
) -> BasinsTable:
    """Charge la couche des sous-bassins (polygones + coordonnées d'exutoire).

    Accepte soit le GeoPackage/shapefile des polygones
    (``hybas_af_lev05_with_outlets.gpkg``), soit le CSV plat des seules
    coordonnées d'exutoire (``hybas_af_lev05_outlet_coordinates.csv``) --
    dans ce dernier cas, seule ``method="outlet"`` sera utilisable (pas de
    géométrie de bassin pour ``"intersects"``/``"within"``).
    """
    gpd = _require_geopandas()
    path = Path(basins_path)
    if not path.is_file():
        raise FileNotFoundError(f"Fichier des sous-bassins introuvable : {path}")

    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
        columns = list(df.columns)
        lon_c = lon_col or _pick_column(columns, _LON_CANDIDATES, "OUTLET_LONGITUDE")
        lat_c = lat_col or _pick_column(columns, _LAT_CANDIDATES, "OUTLET_LATITUDE")
        id_c = id_col or _pick_column(columns, _ID_CANDIDATES, "identifiant de bassin")
        geometry = gpd.points_from_xy(df[lon_c], df[lat_c])
        gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")
        gdf["outlet_geometry"] = gdf.geometry
        has_polygon = False
    else:
        gdf = gpd.read_file(path, layer=layer)
        gdf = _ensure_wgs84(gdf, str(path))
        columns = [c for c in gdf.columns if c != "geometry"]
        lon_c = lon_col or _pick_column(columns, _LON_CANDIDATES, "OUTLET_LONGITUDE")
        lat_c = lat_col or _pick_column(columns, _LAT_CANDIDATES, "OUTLET_LATITUDE")
        id_c = id_col or _pick_column(columns, _ID_CANDIDATES, "identifiant de bassin")
        gdf["outlet_geometry"] = gpd.points_from_xy(gdf[lon_c], gdf[lat_c])
        geom_types = set(gdf.geometry.geom_type.dropna().unique())
        has_polygon = bool(geom_types) and geom_types.issubset({"Polygon", "MultiPolygon"})
        if not has_polygon:
            LOG.warning(
                "%s : géométrie non polygonale (%s) -- seule method='outlet' sera utilisable.",
                path, geom_types or "aucune",
            )

    return BasinsTable(gdf=gdf, id_col=id_c, lon_col=lon_c, lat_col=lat_c, has_polygon=has_polygon, source=str(path))


def select_basins_in_zone(
    zone: Zone,
    basins: str | Path | BasinsTable = DEFAULT_BASINS_SOURCE,
    *,
    method: str = "outlet",
    id_col: str | None = None,
    lon_col: str | None = None,
    lat_col: str | None = None,
    layer: str | None = None,
):
    """Sélectionne les sous-bassins tombant dans ``zone`` selon ``method``.

    Retourne ``(selected_gdf, table)`` -- ``selected_gdf`` est le
    sous-ensemble filtré (mêmes colonnes que la couche source), ``table`` est
    le ``BasinsTable`` complet (utile pour ``basins_to_points``, qui a besoin
    des noms de colonnes résolus).
    """
    if method not in METHODS:
        raise ValueError(f"method doit valoir l'un de {METHODS} (reçu : {method!r})")

    table = basins if isinstance(basins, BasinsTable) else load_basins(
        basins, id_col=id_col, lon_col=lon_col, lat_col=lat_col, layer=layer
    )

    if method == "outlet":
        mask = table.gdf["outlet_geometry"].within(zone.geometry)
    else:
        if not table.has_polygon:
            raise ValueError(
                f"method={method!r} nécessite la géométrie des sous-bassins (polygones) -- "
                f"utilisez le GeoPackage/shapefile complet des bassins ({table.source} n'en a pas), "
                "ou method='outlet'."
            )
        mask = table.gdf.geometry.intersects(zone.geometry) if method == "intersects" else table.gdf.geometry.within(zone.geometry)

    selected = table.gdf.loc[mask].reset_index(drop=True)
    LOG.info(
        "Zone %s : %d/%d sous-bassin(s) sélectionné(s) (method=%s, source=%s)",
        zone.label, len(selected), len(table.gdf), method, table.source,
    )
    if selected.empty:
        LOG.warning(
            "Aucun sous-bassin sélectionné pour la zone %s (method=%s) -- vérifiez la zone, "
            "la source des bassins, ou essayez method='intersects' (plus permissif que 'within').",
            zone.label, method,
        )
    return selected, table


def basins_to_points(
    gdf,
    *,
    id_col: str | None = None,
    lon_col: str | None = None,
    lat_col: str | None = None,
) -> pd.DataFrame:
    """Convertit une sélection de sous-bassins en table de points ``ID, LONG, LAT``.

    Colonnes en sortie : ``ID`` (str, à partir de l'identifiant de bassin),
    ``LONG``/``LAT`` (coordonnées d'exutoire), puis les autres colonnes
    d'attributs conservées telles quelles (géométrie(s) exclue(s)) -- ce
    format est directement compatible avec ``glofas_extract.read_points_csv``
    (colonnes ``ID``/``LONG``/``LAT`` reconnues automatiquement).
    """
    columns = [c for c in gdf.columns if c not in ("geometry", "outlet_geometry")]
    id_c = id_col or _pick_column(columns, _ID_CANDIDATES, "identifiant de bassin")
    lon_c = lon_col or _pick_column(columns, _LON_CANDIDATES, "OUTLET_LONGITUDE")
    lat_c = lat_col or _pick_column(columns, _LAT_CANDIDATES, "OUTLET_LATITUDE")

    out = pd.DataFrame(gdf[columns]).copy()
    out.insert(0, "LAT", out.pop(lat_c).astype(float))
    out.insert(0, "LONG", out.pop(lon_c).astype(float))
    out.insert(0, "ID", out.pop(id_c).astype(str))
    return out.reset_index(drop=True)


def export_points_csv(points_df: pd.DataFrame, output: str | Path) -> Path:
    """Écrit ``points_df`` (sortie de ``basins_to_points``) au format CSV."""
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    points_df.to_csv(output_path, index=False)
    LOG.info("Écrit : %s (%d point(s))", output_path, len(points_df))
    return output_path


# --------------------------------------------------------------------------- #
# Pipeline complet
# --------------------------------------------------------------------------- #


def build_zone_points(
    *,
    iso3: str | None = None,
    boundary_path: str | Path | None = None,
    countries_path: str | Path = DEFAULT_COUNTRIES_GPKG,
    country_col: str = DEFAULT_COUNTRY_COL,
    basins_path: str | Path = DEFAULT_BASINS_SOURCE,
    method: str = "outlet",
    id_col: str | None = None,
    lon_col: str | None = None,
    lat_col: str | None = None,
    buffer_km: float = 0.0,
    output: str | Path | None = None,
) -> tuple[Zone, pd.DataFrame]:
    """Enchaîne résolution de zone -> sélection des bassins -> export CSV.

    C'est le point d'entrée le plus simple pour préparer le fichier de
    points en une seule fois : ``iso3="CMR"`` (ou ``boundary_path=...``),
    ``output="resultats/points_cmr.csv"``, et c'est ce fichier qui est
    ensuite passé à ``glofas_download``/``glofas_extract``/``glofas_forecast``.
    """
    zone = resolve_zone(
        iso3=iso3, boundary_path=boundary_path, countries_path=countries_path,
        country_col=country_col, buffer_km=buffer_km,
    )
    selected, table = select_basins_in_zone(
        zone, basins_path, method=method, id_col=id_col, lon_col=lon_col, lat_col=lat_col,
    )
    points = basins_to_points(selected, id_col=table.id_col, lon_col=table.lon_col, lat_col=table.lat_col)
    if output is not None:
        export_points_csv(points, output)
    return zone, points


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sélectionne les sous-bassins HydroBASINS tombant dans une zone d'étude "
            "(code ISO3 ou shapefile/GeoPackage personnalisé) et exporte leurs "
            "exutoires au format ID,LONG,LAT (points.csv)."
        )
    )
    zone_group = parser.add_mutually_exclusive_group(required=True)
    zone_group.add_argument("--iso3", help="Code pays ISO3 (ex. CMR, NGA, TCD) -- recherché dans --countries")
    zone_group.add_argument("--boundary", help="Chemin vers un shapefile/GeoPackage/GeoJSON de la zone d'étude")

    parser.add_argument("--countries", default=DEFAULT_COUNTRIES_GPKG, help="Limites administratives (défaut : %(default)s)")
    parser.add_argument("--country-col", default=DEFAULT_COUNTRY_COL, help="Colonne code pays (défaut : %(default)s)")
    parser.add_argument("--basins", default=DEFAULT_BASINS_SOURCE, help="Sous-bassins HydroBASINS (défaut : %(default)s)")
    parser.add_argument("--method", choices=METHODS, default="outlet", help="Critère de sélection (défaut : %(default)s)")
    parser.add_argument("--buffer-km", type=float, default=0.0, help="Marge (km) ajoutée autour de la zone")
    parser.add_argument("--output", required=True, help="CSV de points en sortie (colonnes ID,LONG,LAT)")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    try:
        zone, points = build_zone_points(
            iso3=args.iso3,
            boundary_path=args.boundary,
            countries_path=args.countries,
            country_col=args.country_col,
            basins_path=args.basins,
            method=args.method,
            buffer_km=args.buffer_km,
            output=args.output,
        )
    except Exception as exc:
        LOG.error("%s", exc)
        return 1
    print(f"Zone : {zone.label} ({zone.source}) -- {len(points)} point(s) écrits dans {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
