# Boîte à outils GLOFAS (téléchargement, extraction, visualisation, prévision/risque)

Scripts utilisables individuellement (en notebook ou en ligne de commande)
ou via un point d'entrée unique `glofas_cli.py` :

| Script | Rôle |
|---|---|
| `glofas_basins.py` | Sélectionne automatiquement les sous-bassins HydroBASINS d'une zone d'étude (code pays ISO3 ou shapefile/GeoPackage personnalisé) et génère le CSV de points (`ID, LONG, LAT`) -- voir § 8. |
| `glofas_download.py` | Télécharge l'historique GloFAS depuis l'EWDS (fichiers mensuels). |
| `glofas_extract.py` | Extrait les séries temporelles à des points précis (CSV `ID, LONG, LAT`), avec recalage sur le réseau hydrographique dans un rayon donné. |
| `glofas_visualize.py` | Génère une carte interactive et un rapport HTML à partir des résultats de l'extraction ; carte(s) de risque d'inondation (voir § 7 et § 8). |
| `glofas_forecast.py` | Téléchargement et extraction de la **prévision** GloFAS (30 jours, 51 membres) -- voir § 7. |
| `glofas_risk.py` | Seuils de risque par quantile historique (période configurable) et classification de la prévision -- voir § 7. |
| `glofas_cli.py` | Point d'entrée unique : `python glofas_cli.py <basins\|download\|extract\|visualize\|download-forecast\|extract-forecast\|risk> ...` (mêmes options que le script correspondant). |

## Cloner ce dépôt

```powershell
git clone <URL-du-dépôt> glofas-atelier-noaa
cd glofas-atelier-noaa
conda env create -f environment.yml
```

Structure du dépôt :

```
glofas-atelier-noaa/
├── glofas_basins.py        Sélection automatique de zone/sous-bassins (§ 8)
├── glofas_download.py      Téléchargement historique GloFAS (§ 3)
├── glofas_extract.py       Extraction aux points (§ 4)
├── glofas_visualize.py     Cartes et rapport HTML (§ 5, § 7, § 8)
├── glofas_forecast.py      Téléchargement/extraction de la prévision (§ 7)
├── glofas_risk.py          Seuils de risque et classification (§ 7)
├── glofas_cli.py           Point d'entrée unique en ligne de commande
├── environment.yml / requirements.txt / setup_conda.ps1
├── download_glofas_data.ipynb   Notebook : téléchargement + extraction + visualisation
├── previsions_risque.ipynb      Notebook : prévision et cartes de risque
├── zone_etude_risque.ipynb      Notebook : sélection de zone + cartes de risque cadrées
├── static/                 Couches géographiques nécessaires (§ 8)
├── gr4j/                   Modélisation pluie-débit GR4J — bonus, voir § 6
└── docs/
    └── agenda_glofas_atelier_noaa.docx   Agenda de la formation
```

Les notebooks sont fournis **sans sorties d'exécution** (cellules à
ré-exécuter) : ouvrez-les dans JupyterLab (`jupyter lab`, une fois
l'environnement activé) et lancez les cellules dans l'ordre.

## 1. Créer l'environnement Conda

Depuis PowerShell, dans ce dossier :

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup_conda.ps1
conda activate glofas-ewds
```

`environment.yml` installe aussi `jupyterlab` et `ipykernel`, donc
`jupyter lab` est disponible directement une fois l'environnement activé :

```powershell
conda activate glofas-ewds
jupyter lab
```

Si `jupyter` reste introuvable (`'jupyter' is not recognized...`), c'est que
l'environnement a été créé avant l'ajout de ces deux paquets : relancez la
mise à jour ci-dessous, puis ouvrez un **nouveau** terminal PowerShell avant
de réactiver l'environnement (le PATH d'un terminal déjà ouvert n'est pas
rafraîchi automatiquement).

Il est aussi possible d'utiliser directement le fichier YAML :

```powershell
conda env create -f environment.yml
conda activate glofas-ewds
```

Pour mettre ultérieurement l'environnement à jour :

```powershell
conda env update -n glofas-ewds -f environment.yml --prune
```

`environment.yml` installe aussi `xarray`, `cfgrib`, `eccodes`, `netcdf4`,
`numpy` et `pandas`, nécessaires à `glofas_extract.py` (extraction ponctuelle
aux points d'intérêt), ainsi que `geopandas`, `pyogrio` et `shapely`,
nécessaires à `glofas_basins.py` (sélection par zone d'étude -- § 8). Si vous
obtenez `ModuleNotFoundError: No module named 'cfgrib'` (ou `'geopandas'`),
relancez la commande `conda env update` ci-dessus pour mettre à jour un
environnement créé avant l'ajout de ces dépendances (ecCodes doit passer par
conda-forge : une installation `pip install cfgrib` seule ne suffit
généralement pas sous Windows, faute de bibliothèque ecCodes). Vérification :

```powershell
conda run -n glofas-ewds python -m cfgrib selfcheck
conda run -n glofas-ewds python -c "import geopandas; print(geopandas.__version__)"
```

## 2. Configurer l'accès EWDS

Créer le fichier `$HOME\.cdsapirc` :

```yaml
url: https://ewds.climate.copernicus.eu/api
key: VOTRE_CLE_PERSONNELLE
```

La clé est disponible dans le profil EWDS. Il faut également accepter la licence
du jeu de données `cems-glofas-historical` sur le portail avant la première requête.

Ne jamais placer la vraie clé API dans ce dépôt ou dans un script Python.

## 3. Lancer le téléchargement

```powershell
python glofas_download.py --years 1980-1989
```

Exemple limité à une zone et à trois mois :

```powershell
python glofas_download.py `
  --years 1980-1985 `
  --months 1 2 3 `
  --area 15 -20 -5 20 `
  --output-dir glofas_afrique_ouest
```

Sans activation explicite de l'environnement :

```powershell
conda run -n glofas-ewds python glofas_download.py --years 1980-1989
```

## 4. Extraire les données à des points précis

Une fois les fichiers téléchargés, `glofas_extract.py` extrait les séries
temporelles de débit aux coordonnées d'un CSV utilisateur (colonnes `ID`,
`LONG`, `LAT` — voir `points_exemple.csv`). Le paramètre `--radius-km`
élargit la recherche autour de chaque point pour compenser des coordonnées
qui ne tombent pas exactement sur le réseau hydrographique simulé : la
maille retenue est alors celle de plus fort débit dans ce rayon.

```powershell
python glofas_extract.py `
  --points points.csv `
  --input-dir glofas_data `
  --start 1980-01 --end 1983-12 `
  --radius-km 10 `
  --method max `
  --output resultats/extraction `
  --report
```

`--report` génère en plus, dans la foulée, la carte interactive et le
rapport HTML décrits ci-dessous (équivalent à enchaîner avec
`glofas_visualize.py`). Sans `--report`, seuls `resultats/extraction_series.csv`
(séries temporelles) et `resultats/extraction_points.csv` (résumé du
recalage par point : maille retenue, distance, statut) sont écrits.

Équivalent via le point d'entrée unique :

```powershell
python glofas_cli.py extract --points points.csv --input-dir glofas_data --start 1980-01 --end 1983-12 --radius-km 10 --output resultats/extraction --report
```

## 5. Visualiser les résultats

`glofas_visualize.py` transforme les sorties de l'extraction en trois
fichiers HTML autonomes (à garder dans le même dossier, ils se référencent
entre eux) :

- `*_carte.html` — carte interactive (Leaflet/OpenStreetMap) : point saisi,
  maille GloFAS retenue, rayon de recherche, code couleur selon la qualité
  du recalage (vert = maille la plus proche, orange = recalée dans le rayon,
  rouge = repli faute de maille valide dans le rayon).
- `*_series.html` — graphique interactif des séries temporelles (zoom,
  survol, une couleur par point). Autonome, consultable **hors connexion**.
- `*_rapport.html` — page unique combinant la carte, un tableau récapitulatif
  et le graphique.

```powershell
python glofas_visualize.py `
  --series resultats/extraction_series.csv `
  --points resultats/extraction_points.csv `
  --output resultats/extraction
```

Ou directement en une seule commande via `--report` sur `glofas_extract.py`
(ci-dessus), ou en notebook via `extract_glofas_at_points(..., make_report=True)`.

**Remarque connexion** : la carte a besoin d'Internet pour charger la
bibliothèque Leaflet et les fonds de carte (OpenStreetMap/Esri) — normalement
disponible puisque le téléchargement GloFAS lui-même nécessite l'accès à
l'EWDS. Le graphique des séries temporelles, lui, fonctionne entièrement hors
ligne (bibliothèque Plotly intégrée au fichier).

Nécessite `folium` et `plotly` (déjà inclus dans `environment.yml` /
`requirements.txt` ; si votre environnement a été créé avant leur ajout,
relancez `conda env update -n glofas-ewds -f environment.yml --prune`).

### Choisir les stations et la période affichées

`build_timeseries_figure` accepte des filtres optionnels, et écrit le
graphique filtré si `output_html` est fourni :

```python
from glofas_visualize import build_timeseries_figure

fig = build_timeseries_figure(
    "resultats/extraction_series.csv",
    ids=["S01", "S02"],            # None (ou omis) = toutes les stations
    start="1981-01", end="1981-12",
    output_html="resultats/extraction_series_1981_S01_S02.html",
)
```

Équivalent en ligne de commande (écrit en plus `<output>_series_filtre.html`,
sans modifier les fichiers standards) :

```powershell
python glofas_visualize.py `
  --series resultats/extraction_series.csv --points resultats/extraction_points.csv `
  --output resultats/extraction `
  --ids S01 S02 --start 1981-01 --end 1981-12
```

En notebook, `interactive_timeseries_explorer("resultats/extraction_series.csv")`
affiche un petit tableau de bord (liste de sélection des stations, deux
sélecteurs de date, bouton « Enregistrer le graphique ») pour choisir et
sauvegarder sans réécrire de code à chaque fois — pratique en démonstration.
Nécessite `ipywidgets` (déjà inclus dans `environment.yml` /
`requirements.txt`).

## 6. Modélisation hydrologique avec GR4J (R)

> **Module bonus / optionnel** — ce module n'est pas couvert par l'agenda officiel de la formation (2 jours, voir `docs/agenda_glofas_atelier_noaa.docx`), qui se concentre sur GloFAS. Il reste disponible pour les participants intéressés par la modélisation pluie-débit.

Dossier `gr4j/` : script de calage/validation du modèle pluie-débit GR4J
(package R `airGR`), simplifié pour la démonstration en atelier — une seule
période de calage et une de validation (pas de validation croisée), calage
rapide via `Calibration_Michel` (pas d'optimiseur externe type DEoptim).

| Fichier | Rôle |
|---|---|
| `gr4j_modelling.R` | Script principal : chargement, contrôle des données, calage, validation, graphiques (diagnostic airGR, hydrogramme, obs. vs simulé), export CSV/PNG. |
| `gr4j_modelling.ipynb` | Le même enchaînement, en notebook Jupyter (noyau R) — une cellule par étape, pratique pour la démonstration en atelier (voir « Notebook » ci-dessous). |
| `generer_donnees_exemple.py` | Génère `donnees_exemple_gr4j.csv`, un jeu de données **fictif** (climat et débit simulés simplement) pour tester le script dès maintenant. |
| `donnees_exemple_gr4j.csv` | Le jeu de données fictif lui-même (déjà généré). |

```powershell
conda activate glofas-ewds
Rscript gr4j/gr4j_modelling.R
```

(ou depuis RStudio, en ouvrant `gr4j_modelling.R` — les paramètres à
adapter à vos données sont regroupés en tête de script, section
« PARAMÈTRES UTILISATEUR ».)

### Notebook (`gr4j_modelling.ipynb`)

Ce notebook nécessite un noyau Jupyter **R** (`IRkernel`), différent du
noyau Python utilisé par `download_glofas_data.ipynb`. Il n'est pas inclus
dans l'environnement Conda `glofas-ewds` (Python) : installez-le une fois,
dans une console R (RStudio ou `R.exe`) :

```r
install.packages(c("IRkernel", "airGR", "dplyr", "tidyr", "lubridate", "readr", "ggplot2", "hydroGOF"))
IRkernel::installspec(name = "ir", displayname = "R")
```

Ensuite, dans Jupyter Lab (lancé depuis l'environnement `glofas-ewds` comme
pour `download_glofas_data.ipynb`), ouvrez `gr4j/gr4j_modelling.ipynb` et
sélectionnez le noyau « R ». Si le noyau « R » n'apparaît pas dans la liste
après l'installation, vérifiez avec `jupyter kernelspec list` qu'il a bien
été enregistré au même endroit que les noyaux utilisés par cette
installation de Jupyter.

Chaque graphique du notebook est à la fois affiché à l'écran et enregistré
en PNG dans `resultats_gr4j/`, comme dans le script.

**Données d'entrée** : le script attend un CSV `date, pcp, evap, debit`
(précipitation et ETP en mm/j, débit observé en m3/s). Le notebook de
préparation de ces données à partir de GloFAS (débit de référence) et d'une
réanalyse (pluie/ETP) n'est pas encore développé — ce script prend le CSV
déjà prêt en entrée, quelle que soit sa source ultérieure ; en attendant,
`donnees_exemple_gr4j.csv` permet de vérifier que tout l'enchaînement
fonctionne.

**Prérequis R** (à ajouter à votre installation R/RStudio — indépendants de
l'environnement Conda `glofas-ewds`, qui reste pour les scripts Python) :

```r
install.packages(c("airGR", "dplyr", "tidyr", "lubridate", "readr", "ggplot2", "hydroGOF"))
```

**Remarque** : ce script n'a pas pu être testé contre le package `airGR` réel
côté génération (pas d'accès à CRAN dans cet environnement) — il reprend
l'enchaînement de fonctions déjà validé dans le script dont il s'inspire.
Merci de le tester une première fois de votre côté avant l'atelier.

## 7. Prévisions GloFAS et cartes de risque d'inondation

Objectif : à partir de la prévision GloFAS du jour, produire une **carte de
risque d'inondation** par point d'intérêt et par échéance (**normal**,
**risque faible**, **risque modéré**, **risque sévère**), en comparant la
prévision à des **seuils de quantile calculés sur l'historique** de chaque
point -- à défaut de disposer des seuils de risque "officiels" (périodes de
retour 2/5/20 ans, calées par ajustement statistique, comme le fait GloFAS
en opérationnel).

Notebook de démonstration : `previsions_risque.ipynb` (à exécuter après
`download_glofas_data.ipynb`, dont il réutilise `resultats/extraction_points.csv`
et `resultats/extraction_series.csv`).

### Téléchargement de la prévision (`glofas_forecast.py`)

GloFAS propose sur l'EWDS le jeu de données `cems-glofas-forecast` :
horizon de **30 jours**, mis à jour **chaque jour**, ensemble de **51
membres** (1 prévision de contrôle + 50 membres perturbés).

```powershell
python glofas_forecast.py download `
  --issue-dates 2026-09-04 `
  --max-days 15 `
  --area 15 -20 -5 20 `
  --products control perturbed `
  --output-dir glofas_forecast_data
```

Un fichier par date d'émission et par type de membre :
`glofas_forecast_{AAAAMMJJ}_{control|perturbed}.{ext}`. `--products control`
seul (sans `perturbed`) accélère nettement le téléchargement pour une
simple démonstration, au prix de ne plus pouvoir calculer de probabilité de
dépassement (un seul membre). Comme pour l'historique, restreignez `--area`
à votre zone d'intérêt.

### Extraction aux points (`glofas_forecast.py extract`)

**Important** : contrairement à l'extraction historique, ceci ne recale
**pas** à nouveau les points sur le réseau hydrographique -- la maille déjà
déterminée par `glofas_extract.py` (colonnes `lon_pixel`/`lat_pixel` de
`*_points.csv`) est réutilisée telle quelle, pour comparer la prévision aux
seuils historiques sur exactement la même maille (un recalage indépendant
sur la prévision, qui reflète la situation du jour et non un débit moyen,
pourrait sélectionner une maille différente et fausser la comparaison).

```powershell
python glofas_forecast.py extract `
  --points-meta resultats/extraction_points.csv `
  --forecast-dir glofas_forecast_data `
  --output resultats/prevision_series.csv
```

### Seuils de risque et classification (`glofas_risk.py`)

```powershell
python glofas_cli.py risk seuils `
  --historique resultats/extraction_series.csv `
  --basis daily `
  --debut 1990-01-01 --fin 2020-12-31 `
  --output resultats/seuils_risque.csv

python glofas_cli.py risk classer `
  --prevision resultats/prevision_series.csv `
  --seuils resultats/seuils_risque.csv `
  --output resultats/prevision_risque.csv
```

`--basis daily` (défaut) calcule les quantiles sur toutes les valeurs
journalières de l'historique ; `--basis annual_max` les calcule sur les
maxima annuels, plus proche de la notion de période de retour mais
nécessite un historique plus long (5 ans minimum, idéalement bien plus --
un avertissement s'affiche sinon). Les trois seuils par défaut (quantiles
0.80 / 0.90 / 0.98, réglables via `--faible`/`--modere`/`--severe`)
délimitent les quatre niveaux de risque. `--debut`/`--fin` (optionnels,
`AAAA-MM-JJ`) restreignent la période historique utilisée pour le calcul --
par défaut (aucun des deux précisé), toute la période disponible dans
`--historique` est utilisée.

La classification (`resultats/prevision_risque.csv`) donne, pour chaque
point et échéance : la statistique centrale de l'ensemble (médiane par
défaut), l'étendue (min/max), la valeur du membre de contrôle, la
probabilité de dépassement de chaque seuil (fraction des membres qui le
dépassent) et la catégorie de risque qui en résulte.

### Carte de risque (`glofas_visualize.py`)

```python
from glofas_visualize import build_risk_map, generate_daily_risk_maps, interactive_risk_explorer

# Une échéance précise (obligatoire si plusieurs échéances sont présentes) :
build_risk_map(
    "resultats/prevision_risque.csv", "resultats/extraction_points.csv",
    output_html="resultats/carte_risque.html", leadtime_hours=72,
)

# Une carte par échéance disponible (J+1, J+2, ...), en une seule commande :
generate_daily_risk_maps(
    "resultats/prevision_risque.csv", "resultats/extraction_points.csv",
    "resultats/cartes_risque_journalieres",
)

# Sélecteur interactif (échéance + date d'émission), en notebook :
interactive_risk_explorer("resultats/prevision_risque.csv", "resultats/extraction_points.csv")
```

Couleurs : vert = normal, jaune = risque faible, orange = risque modéré,
rouge = risque sévère (palette "statut" du projet, jamais la couleur seule
-- toujours accompagnée d'une étiquette dans la légende et les infobulles).
Même remarque connexion que pour la carte historique (§ 5) : nécessite
Internet pour les fonds de carte.

`build_risk_map`/`generate_daily_risk_maps` acceptent aussi un paramètre
optionnel `zone=...` (contour affiché + carte cadrée dessus) -- voir § 8,
où `zone` est justement la zone d'étude utilisée pour sélectionner les
sous-bassins.

Aucune dépendance supplémentaire par rapport aux sections précédentes
(`cdsapi`, `xarray`, `folium`, `ipywidgets`, etc. déjà inclus dans
`environment.yml`).

## 8. Sélection automatique des sous-bassins par zone d'étude

Objectif : éviter de saisir les points d'intérêt à la main. À partir d'une
**zone d'étude** (un pays -- code ISO3 -- ou votre propre shapefile/
GeoPackage), `glofas_basins.py` sélectionne automatiquement les
**sous-bassins HydroBASINS niveau 5** dont l'exutoire (ou le polygone,
selon la méthode choisie) tombe dans cette zone, puis génère le fichier de
points (`ID, LONG, LAT`) attendu par tout le reste de la chaîne
(`glofas_download`/`glofas_extract`/`glofas_forecast`/`glofas_risk`/
`glofas_visualize`, utilisés ensuite **sans aucune modification**).

Notebook de démonstration : `zone_etude_risque.ipynb` (enchaîne cette
sélection avec tout le pipeline historique + prévision + risque + cartes,
en un seul notebook).

### Couches statiques nécessaires (dossier `static/`)

| Fichier | Rôle |
|---|---|
| `afrique.gpkg` | Limites administratives des pays d'Afrique, colonne `GMI_CNTRY` (code ISO3) -- pour résoudre une zone à partir d'un simple code pays. |
| `hybas_af_lev05_with_outlets.gpkg` | Sous-bassins HydroBASINS niveau 5 (polygones), avec les coordonnées de leur exutoire (`OUTLET_LONGITUDE`/`OUTLET_LATITUDE`). Nécessaire pour `--method intersects`/`within`. |
| `hybas_af_lev05_outlet_coordinates.csv` | Équivalent plat (sans les polygones) du fichier précédent -- plus léger, suffisant pour `--method outlet` (défaut). |

### Définir la zone d'étude et sélectionner les sous-bassins

```powershell
# Par code pays ISO3 :
python glofas_cli.py basins `
  --iso3 CMR `
  --basins static/hybas_af_lev05_with_outlets.gpkg `
  --method outlet `
  --output resultats/points_zone.csv

# Par zone personnalisée (shapefile/GeoPackage/GeoJSON) :
python glofas_cli.py basins `
  --boundary mon_bassin_versant.shp `
  --basins static/hybas_af_lev05_with_outlets.gpkg `
  --method intersects `
  --buffer-km 20 `
  --output resultats/points_zone.csv
```

En Python (notebook) :

```python
from glofas_basins import build_zone_points

zone, points = build_zone_points(
    iso3="CMR",                             # ou : boundary_path="mon_bassin.shp"
    basins_path="static/hybas_af_lev05_with_outlets.gpkg",
    method="outlet",
    output="resultats/points_zone.csv",
)
```

`points` (et `resultats/points_zone.csv`) sont ensuite utilisés exactement
comme un CSV de points saisi à la main :

```python
from glofas_download import download_glofas_discharge
from glofas_extract import extract_glofas_at_points

download_glofas_discharge(year=range(1980, 1990), area=(15, 8, -5, 20), output_dir="glofas_data")
extract_glofas_at_points(points="resultats/points_zone.csv", input_dir="glofas_data", output="resultats/extraction_zone")
```

### `--method` : trois critères de sélection

- **`outlet`** (défaut) : le point d'exutoire du sous-bassin tombe dans la
  zone. Ne nécessite **pas** la géométrie des polygones -- fonctionne aussi
  bien avec le GeoPackage complet qu'avec le CSV plat des seules
  coordonnées d'exutoire. C'est aussi la méthode la plus cohérente avec le
  reste de la chaîne, puisque l'extraction GloFAS se fait justement au
  pixel le plus proche de ce point.
- **`intersects`** : le polygone du sous-bassin touche la zone (au moins
  partiellement) -- plus permissif, capture aussi les bassins
  transfrontaliers dont une partie seulement est dans la zone. Nécessite
  la géométrie des polygones (GeoPackage/shapefile, pas le CSV plat).
- **`within`** : le polygone du sous-bassin est entièrement contenu dans la
  zone -- plus strict. Nécessite également la géométrie des polygones.

`--buffer-km` (optionnel) ajoute une marge autour de la zone (utile pour ne
pas exclure un sous-bassin juste à cheval sur la frontière).

### Seuils sur une période historique donnée

`glofas_risk.compute_historical_thresholds` accepte désormais `start`/`end`
(`--debut`/`--fin` en ligne de commande) pour restreindre la période
utilisée pour le calcul des quantiles à une période définie par
l'utilisateur, plutôt que toute la période historique disponible (défaut) --
voir § 7.

### Cartes cadrées sur la zone d'étude

`build_risk_map`/`generate_daily_risk_maps` (§ 7) acceptent un paramètre
`zone=...` (le `Zone` retourné par `resolve_zone`/`build_zone_points`, un
GeoDataFrame geopandas, ou une géométrie shapely) : la zone est alors
affichée en contour sur la carte, et la carte y est cadrée automatiquement.
`generate_daily_risk_maps` produit une carte HTML par échéance disponible
(« cartes journalières ») :

```python
from glofas_visualize import generate_daily_risk_maps

generate_daily_risk_maps(
    "resultats/prevision_risque_zone.csv", "resultats/extraction_zone_points.csv",
    "resultats/cartes_risque_zone",
    zone=zone,   # le Zone retourné par build_zone_points / resolve_zone
)
```

Dépendance supplémentaire : `geopandas` (+ `pyogrio`, `shapely`) -- voir § 1.

