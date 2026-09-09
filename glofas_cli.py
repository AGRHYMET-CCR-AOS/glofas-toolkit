"""Point d'entrée unique de la boîte à outils GloFAS.

Regroupe les différents scripts derrière une seule commande à
sous-commandes, pour n'avoir qu'un seul point d'entrée à retenir :

    python glofas_cli.py basins              --iso3 CMR --basins static/hybas_af_lev05_with_outlets.gpkg --output resultats/points_cmr.csv
    python glofas_cli.py download            --points resultats/points_cmr.csv --years 1980-1989 --area 7 8 2 16 --output-dir glofas_data
    python glofas_cli.py extract             --points resultats/points_cmr.csv --input-dir glofas_data --radius-km 10 --output resultats/extraction --report
    python glofas_cli.py visualize           --series resultats/extraction_series.csv --points resultats/extraction_points.csv --output resultats/extraction
    python glofas_cli.py download-forecast   --issue-dates 2026-09-04 --area 7 8 2 16 --output-dir glofas_forecast_data
    python glofas_cli.py extract-forecast    --points-meta resultats/extraction_points.csv --forecast-dir glofas_forecast_data --output resultats/prevision_series.csv
    python glofas_cli.py risk seuils         --historique resultats/extraction_series.csv --output resultats/seuils_risque.csv
    python glofas_cli.py risk classer        --prevision resultats/prevision_series.csv --seuils resultats/seuils_risque.csv --output resultats/prevision_risque.csv

Chaque sous-commande accepte exactement les mêmes options que le script
correspondant (voir ``python glofas_cli.py <sous-commande> --help``) ; ce
point d'entrée se contente de router les arguments vers la fonction
``main`` (ou ``main_download``/``main_extract`` pour ``glofas_forecast``)
du module concerné. Tous les scripts restent utilisables individuellement
(et importables depuis un notebook), ceci n'est qu'un raccourci pour
l'usage en ligne de commande.

``basins`` prépare le fichier de points (``ID,LONG,LAT``) à partir d'une
zone d'étude (code pays ISO3 ou shapefile/GeoPackage personnalisé) et des
sous-bassins HydroBASINS -- voir ``glofas_basins.py`` -- c'est le point de
départ naturel de tout le reste. ``download``/``extract``/``visualize``
portent sur l'historique GloFAS ; ``download-forecast``/``extract-forecast``/
``risk`` portent sur la prévision et la classification en niveaux de risque
(voir ``glofas_forecast.py`` et ``glofas_risk.py``).
"""

from __future__ import annotations

import sys
from typing import Sequence

COMMANDS = ("basins", "download", "extract", "visualize", "download-forecast", "extract-forecast", "risk")


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0 if argv else 2

    command, rest = argv[0], argv[1:]

    if command == "basins":
        from glofas_basins import main as basins_main

        return basins_main(rest)
    if command == "download":
        from glofas_download import main as download_main

        return download_main(rest)
    if command == "extract":
        from glofas_extract import main as extract_main

        return extract_main(rest)
    if command == "visualize":
        from glofas_visualize import main as visualize_main

        return visualize_main(rest)
    if command == "download-forecast":
        from glofas_forecast import main_download

        return main_download(rest)
    if command == "extract-forecast":
        from glofas_forecast import main_extract

        return main_extract(rest)
    if command == "risk":
        from glofas_risk import main as risk_main

        return risk_main(rest)

    print(
        f"Commande inconnue : {command!r} (attendu : {', '.join(COMMANDS)})\n\n{__doc__}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
