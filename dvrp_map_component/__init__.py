"""
dvrp_map_component — Composant Streamlit personnalisé (React) : carte des tournées
DVRP + tableau de bord complet, TOUT calculé et affiché côté navigateur.

C'est un VRAI composant Streamlit (protocole officiel `components.declare_component`),
pas un simple `components.html()` : le frontend (dans frontend/build/) est une app
React compilée avec esbuild, qui communique avec Python via le protocole
bidirectionnel standard de Streamlit (`Streamlit.setComponentValue`, `args`, etc.).

Le bundle est PRÉ-COMPILÉ (frontend/build/bundle.js + bundle.css) et committé dans le
repo : Streamlit Cloud sert ces fichiers statiques tels quels, sans étape de build
npm côté serveur — aucune configuration supplémentaire nécessaire au déploiement.

Python calcule les itinéraires UNE SEULE FOIS (à chaque changement réel de paramètres
ou d'événement) et transmet toutes les commandes (avec leur `release_time`) au
composant : ensuite, tant qu'aucune action n'est prise, AUCUNE resynchronisation
Python n'a lieu — l'horloge, le déplacement des camions, l'apparition des commandes,
les KPI et le tableau sont entièrement calculés et affichés par React. Le composant ne
renvoie une valeur à Python (déclenchant un rerun léger) que lorsque l'ensemble des
commandes livrées change réellement — pas de sondage périodique.

Pour modifier le composant : éditez frontend/src/index.jsx, puis
`cd frontend && npm install && npm run build` avant de commit/push.
"""
import os

import streamlit.components.v1 as components

_BUILD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "build")

# Vérification explicite : sur Streamlit Cloud, l'erreur la plus fréquente pour un
# composant custom est que le dossier `frontend/build/` (bundle.js, bundle.css,
# index.html) n'a PAS été poussé sur le dépôt Git — souvent parce qu'un
# `.gitignore` généré par un template de composant exclut `build/` ou
# `node_modules/` par défaut. Sans ce dossier, `declare_component` échoue avec un
# message peu clair (FileNotFoundError profond dans Streamlit). On préfère lever
# ici une erreur explicite qui pointe directement la cause probable.
_REQUIRED_FILES = ("index.html", "bundle.js", "bundle.css")
_missing = [f for f in _REQUIRED_FILES if not os.path.isfile(os.path.join(_BUILD_DIR, f))]
if _missing:
    raise FileNotFoundError(
        f"dvrp_map_component : fichier(s) manquant(s) dans {_BUILD_DIR} : {_missing}. "
        "Cause la plus probable sur Streamlit Cloud : le dossier "
        "'dvrp_map_component/frontend/build/' n'est pas commité sur le dépôt Git "
        "(souvent exclu par un .gitignore contenant 'build/' ou 'node_modules/'). "
        "Vérifiez sur GitHub que ces 3 fichiers sont bien présents dans ce dossier, "
        "et retirez toute règle .gitignore qui exclurait 'dvrp_map_component/frontend/build/'."
    )

_dvrp_map_component = components.declare_component("dvrp_map", path=_BUILD_DIR)


def dvrp_map(
    depot_coords,
    orders,
    cancelled_orders,
    trucks,
    sim_clock_start_min: float,
    sim_minutes_per_real_second: float,
    auto_run: bool,
    planned_distance_km: float,
    baseline_distance_km: float,
    gain_pct: float,
    num_vehicles_used: int,
    num_vehicles_total: int,
    height: int = 520,
    key: str | None = None,
    already_delivered_ids: list | None = None,
    already_traveled_km: float = 0.0,
    instant_finish: bool = False,
):
    """
    depot_coords : (lat, lon)
    orders : commandes routables — liste de dicts {id, client, lat, lon, demand_kg,
        temp_max, time_window, priority, release_time, is_new}
    cancelled_orders : commandes annulées — liste de dicts {id, client, demand_kg,
        priority} (affichées seulement dans le tableau, pas sur la carte)
    trucks : liste de dicts, un par camion physique :
        label, color, used (bool), shape ([[lat,lon],...]), trip_shapes (liste de
        tracés), stops (liste de {order_id, cum_km} dans l'ordre de la tournée),
        total_km, speed_kmh
    sim_clock_start_min : valeur de l'horloge de simulation au moment de l'envoi —
        point de départ à partir duquel React fait avancer le temps lui-même
    sim_minutes_per_real_second : facteur d'accélération du temps
    auto_run : si True, React fait avancer l'horloge en continu ; sinon elle reste figée
    planned_distance_km, baseline_distance_km, gain_pct : preuve d'optimisation
        (calculée une fois par Python), affichée dans le panneau "Données finales"
    num_vehicles_used, num_vehicles_total : pour le KPI "camions en tournée"

    already_delivered_ids : liste des identifiants déjà livrés selon Python
        (st.session_state.delivered_ids — historique fiable, jamais réinitialisé).
        Sert à AMORCER le compteur "commandes livrées" du composant, pour qu'il
        reflète le total depuis le début de la simulation et non seulement les
        commandes livrées depuis le dernier recalcul OR-Tools.
    already_traveled_km : kilométrage déjà parcouru par la flotte selon Python
        (checkpoint conservé entre deux recalculs). Sert à AMORCER le compteur
        "distance parcourue" du composant pour la même raison.
    instant_finish : si True (mode statique ou dynamique sans tracking, où aucune
        position GPS n'est jamais simulée), force l'affichage immédiat du panneau
        "🎉 résultats finaux" — sans quoi il ne s'afficherait jamais, puisqu'il
        dépend normalement d'une distance parcourue simulée qui reste à 0 sans
        tracking. N'affecte ni la position des camions ni le statut des commandes
        (aucune livraison n'est marquée automatiquement).

    Retourne un dict {"delivered_ids": [...], "all_finished": bool,
    "distance_parcourue_km": float, ...} ou None tant que rien n'a encore été
    livré — sert à mettre à jour la liste des commandes annulables et à
    persister le checkpoint de distance pour le prochain recalcul.
    """
    return _dvrp_map_component(
        depot=list(depot_coords),
        orders=orders,
        cancelled_orders=cancelled_orders,
        trucks=trucks,
        sim_clock_start_min=sim_clock_start_min,
        sim_minutes_per_real_second=sim_minutes_per_real_second,
        auto_run=auto_run,
        planned_distance_km=planned_distance_km,
        baseline_distance_km=baseline_distance_km,
        gain_pct=gain_pct,
        num_vehicles_used=num_vehicles_used,
        num_vehicles_total=num_vehicles_total,
        height=height,
        key=key,
        already_delivered_ids=list(already_delivered_ids or []),
        already_traveled_km=already_traveled_km,
        instant_finish=instant_finish,
        default=None,
    )

