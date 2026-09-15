"""
Système Intelligent de Tracking GPS & DVRP Dynamique — Alger
==============================================================

Corrections apportées par rapport à la version générée initialement :

1. BUG BLOQUANT : `np.random.choice(events_pool, p=...)` sur une liste de dicts
   plantait toujours. → corrigé dans event_handler.py (tirage par index).
2. BUG DE SÉCURITÉ DES THREADS : le callback MQTT écrivait directement dans
   st.session_state depuis un thread réseau. → corrigé via une queue.Queue
   thread-safe, vidée uniquement depuis le thread principal (mqtt_manager.py).
3. BUG D'ALIGNEMENT D'INDEX : après avoir filtré `active_orders` par le temps
   écoulé, l'ancien index du DataFrame ne correspondait plus aux positions
   utilisées par OR-Tools (`route[i]`). → corrigé avec `.reset_index(drop=True)`.
4. MANQUE : aucune contrainte de capacité — un camion pouvait se voir assigner
   un poids illimité. → ajout d'une vraie contrainte de capacité (kg).
5. PERFORMANCE : chaque interaction relançait des appels réseau OSRM.
   → mise en cache (st.cache_data, TTL 5 min) dans dvrp_engine.py.
6. ROBUSTESSE : gestion du cas où OR-Tools ne trouve aucune solution
   (capacité totale insuffisante) au lieu de faire planter l'affichage.
7. MQTT désormais optionnel (case à cocher) plutôt que connecté d'office à
   chaque rechargement de page, ce qui évitait d'accumuler des connexions.
8. ÉVÉNEMENTS RÉELLEMENT ACTIFS : la version précédente se contentait de
   JOURNALISER les événements dynamiques (message informatif, sans effet sur
   les données). Ils modifient désormais vraiment l'état de la simulation
   (event_handler.apply_event_effect) — nouvelle commande, annulation, panne
   véhicule, pénalité de trafic, priorité relevée — ce qui déclenche une vraie
   réoptimisation OR-Tools au calcul suivant, au lieu d'un simple message.
9. ROBUSTESSE SUPPLÉMENTAIRE : bouton de réinitialisation des événements
   cumulés, résumé des effets actifs visible en permanence dans la barre
   latérale, et prise en compte du nombre de véhicules réellement disponibles
   (après pannes) dans les indicateurs et les messages d'erreur.
10. ROTATIONS MULTIPLES (capacité insuffisante) : un camion peut désormais
    effectuer plusieurs allers-retours au dépôt si la capacité totale ne
    suffit pas en un seul passage (nombre de rotations calculé automatiquement).
    Modélisé via des véhicules « virtuels » (camion x trajets max) pour
    OR-Tools, regroupés ensuite par camion physique (dvrp_engine.py :
    group_multi_trip_routes). Le temps de rechargement au dépôt entre deux
    rotations n'est pas modélisé (supposé instantané).
11. TRACKING & SIMULATION 100% CÔTÉ NAVIGATEUR (dvrp_map_component/) : Python calcule
    les itinéraires une seule fois par action réelle (démarrage, événement, changement
    de paramètre) et transmet toutes les commandes (avec leur release_time) au composant
    React. Ensuite, l'horloge, le déplacement des camions, l'apparition progressive des
    commandes, les KPI et le tableau sont entièrement calculés et affichés côté
    navigateur — aucune resynchronisation Python périodique pendant que ça tourne. Le
    composant ne renvoie une valeur à Python que lorsqu'une commande est réellement
    livrée (pour la liste des commandes annulables), jamais par sondage.
"""

import math
import time
from datetime import datetime

import numpy as np
import pandas as pd
import pydeck as pdk
import streamlit as st

from dvrp_map_component import dvrp_map
from benchmark_loader import COLUMNS, get_real_algiers_dataset, generate_solomon_benchmark
from dvrp_engine import (
    get_osrm_distance_matrix,
    get_osrm_route_shape,
    group_multi_trip_routes,
    naive_baseline_routes,
    route_distance,
    solve_dvrp_ortools,
)
from event_handler import apply_event_effect, process_dynamic_event
from mqtt_manager import MQTTBridge

st.set_page_config(page_title="DVRP Logistique & Tracking Alger", page_icon="🚚", layout="wide")

# ----------------------------------------------------------------------------
# 1. ÉTAT DE SESSION
# ----------------------------------------------------------------------------
DEFAULTS = {
    "logs": [],
    "mqtt_events": [],
    "mqtt_bridge": None,
    "extra_orders": [],       # commandes ajoutées par des événements NOUVELLE_COMMANDE
    "cancelled_ids": set(),   # commandes retirées (ANNULATION_COMMANDE / CLIENT_ABSENT)
    "priority_overrides": {}, # id -> priorité forcée (ALERTE_TEMPERATURE)
    "vehicle_breakdown_count": 0,  # nb de camions mis hors service (PANNE_VEHICULE)
    "traffic_penalty": 1.0,   # multiplicateur appliqué à la matrice de distances
    "delivered_ids": set(),   # commandes livrées, reçues depuis le composant React
                               # (calculées côté navigateur, pas par Python)
    "traveled_km_checkpoint": 0.0,  # CORRECTIF distance/compteurs erronés : distance
                               # totale déjà parcourue par la flotte depuis le début de
                               # la simulation, conservée entre deux recalculs OR-Tools
                               # (le composant React repart sinon de 0 à chaque recalcul,
                               # voir dvrp_map_component/frontend/src/index.jsx)
    "sim_clock_min": 0.0,     # point de départ de l'horloge transmis au composant React,
                               # qui la fait ensuite avancer lui-même (voir dvrp_map_component)
    "simulation_started": False,  # l'horloge ne bouge pas tant que ce n'est pas True
    "auto_run_active": False,     # état du toggle "Simulation temps réel" (clé du widget)
    "stop_requested": False,  # demande d'arrêt (manuel ou automatique), appliquée au
                               # tout début du prochain script run, AVANT que le toggle
                               # ne soit recréé (Streamlit interdit de modifier la clé
                               # d'un widget après qu'il a déjà été instancié dans le
                               # même run — voir section 3 plus bas).
}
for key, default in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default

# Applique une demande d'arrêt en attente AVANT toute création de widget : c'est le
# seul moment sûr pour modifier st.session_state["auto_run_active"], la clé du toggle
# "Simulation temps réel" plus bas (Streamlit refuse cette modification une fois le
# widget déjà instancié dans le run courant — StreamlitWidgetAlreadyInstantiatedError).
if st.session_state.stop_requested:
    st.session_state.auto_run_active = False
    st.session_state.stop_requested = False


def log_event(message: str) -> None:
    st.session_state.logs.insert(0, f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
    st.session_state.logs = st.session_state.logs[:30]


# ----------------------------------------------------------------------------
# 2. EN-TÊTE
# ----------------------------------------------------------------------------
st.title("🚚 Optimisation logistique DVRP — statique, dynamique, ou avec tracking GPS")
st.caption("Alger — livraison de produits frais / express")
st.markdown("---")

# ----------------------------------------------------------------------------
# 3. BARRE LATÉRALE — MODE DE FONCTIONNEMENT
# ----------------------------------------------------------------------------
# Trois cas d'usage distincts, choisis explicitement par l'utilisateur :
#   - STATIQUE            : VRP classique, un seul calcul sur l'ensemble des commandes
#                            connues dès le départ. Pas d'horloge, pas d'événements,
#                            pas de tracking.
#   - DYNAMIQUE SANS TRACKING/GPS : les commandes/événements évoluent dans le temps
#                            (nouvelles commandes, annulations, pannes, embouteillages...)
#                            et déclenchent une VRAIE réoptimisation OR-Tools, mais
#                            aucune position GPS n'est simulée : les camions ne sont
#                            pas animés sur la carte et aucune télémétrie MQTT n'est
#                            utilisée. La livraison d'une commande est actée
#                            manuellement (bouton), pas détectée par tracking.
#   - DYNAMIQUE AVEC TRACKING : comportement complet d'origine (horloge temps réel,
#                            camions animés selon leur vitesse/tracé réel, MQTT).
MODE_STATIC = "🧊 Statique (calcul unique)"
MODE_DYNAMIC_NO_TRACKING = "🔄 Dynamique — sans tracking / sans GPS"
MODE_DYNAMIC_TRACKING = "📡 Dynamique — avec tracking GPS (temps réel)"

st.sidebar.header("⚙️ Mode de fonctionnement")
run_mode = st.sidebar.radio(
    "Cas d'usage",
    [MODE_STATIC, MODE_DYNAMIC_NO_TRACKING, MODE_DYNAMIC_TRACKING],
    index=2,
)
is_static = run_mode == MODE_STATIC
is_dynamic_no_tracking = run_mode == MODE_DYNAMIC_NO_TRACKING
is_tracking = run_mode == MODE_DYNAMIC_TRACKING

# Changer de cas d'usage repart sur un état propre (une tournée statique et une
# tournée avec tracking en cours ne doivent pas mélanger leurs événements, camions
# en panne, commandes livrées, etc.).
if st.session_state.get("active_run_mode") != run_mode:
    st.session_state.active_run_mode = run_mode
    st.session_state.logs = []
    st.session_state.extra_orders = []
    st.session_state.cancelled_ids = set()
    st.session_state.priority_overrides = {}
    st.session_state.vehicle_breakdown_count = 0
    st.session_state.traffic_penalty = 1.0
    st.session_state.delivered_ids = set()
    st.session_state.traveled_km_checkpoint = 0.0
    st.session_state.sim_clock_min = 0.0
    st.session_state.simulation_started = False
    st.session_state.auto_run_active = False
    st.session_state.stop_requested = False
    if st.session_state.mqtt_bridge is not None:
        st.session_state.mqtt_bridge.stop()
        st.session_state.mqtt_bridge = None
    st.rerun()

st.sidebar.caption({
    MODE_STATIC: "Un seul calcul OR-Tools sur toutes les commandes connues. Pas d'horloge, "
                 "pas d'événements, pas de tracking.",
    MODE_DYNAMIC_NO_TRACKING: "Commandes/événements évoluent dans le temps et redéclenchent "
                               "OR-Tools, mais aucune position GPS n'est simulée (pas de MQTT, "
                               "pas de camions animés). Livraison actée manuellement.",
    MODE_DYNAMIC_TRACKING: "Comportement complet : horloge temps réel, camions animés sur leur "
                            "tracé réel, télémétrie MQTT.",
}[run_mode])
st.sidebar.markdown("---")

# En mode statique, la simulation est réputée « démarrée » d'office : il n'y a pas
# de notion d'horloge à déclencher, le calcul est unique et immédiat.
if is_static:
    st.session_state.simulation_started = True

st.info({
    MODE_STATIC: "🧊 **Mode statique** — toutes les commandes sont connues à l'avance ; "
                 "un seul calcul OR-Tools détermine les tournées optimales.",
    MODE_DYNAMIC_NO_TRACKING: "🔄 **Mode dynamique sans tracking/GPS** — les commandes et "
                               "événements évoluent dans le temps et redéclenchent une "
                               "réoptimisation, mais sans position GPS ni animation des camions.",
    MODE_DYNAMIC_TRACKING: "📡 **Mode dynamique avec tracking GPS** — simulation temps réel "
                            "complète : horloge, camions animés sur leur tracé réel, MQTT.",
}[run_mode], icon="ℹ️")

# ----------------------------------------------------------------------------
# 3bis. BARRE LATÉRALE — DONNÉES
# ----------------------------------------------------------------------------
st.sidebar.header("📁 Jeu de données")
dataset_choice = st.sidebar.selectbox(
    "Source de données",
    ["Cas réel : livraisons Grand Alger", "Benchmark synthétique (type Solomon)"],
)

if dataset_choice.startswith("Cas réel"):
    depot_coords, df_orders = get_real_algiers_dataset()
else:
    depot_coords, df_orders = generate_solomon_benchmark()

st.sidebar.header("🕹 Contrôle de la simulation")
num_vehicles = st.sidebar.slider("Camions frigorifiques disponibles", 1, 4, 2)
vehicle_capacity = st.sidebar.slider("Capacité par camion (kg)", 100, 1000, 400, step=50)
st.sidebar.caption(
    "🔁 Rotations : calculées automatiquement selon la demande totale et la capacité "
    "disponible (pas de réglage manuel nécessaire)."
)

# Valeurs par défaut (utilisées telles quelles en mode statique, et écrasées si
# nécessaire par les widgets ci-dessous dans les modes dynamiques).
truck_speed_kmh = 30
time_accel_label = "Très rapide (1h simulée = 5 min réelles)"
sim_minutes_per_real_second = 60 / (5 * 60)
auto_run = False

if is_static:
    # Pas d'horloge en mode statique : toutes les commandes connues sont considérées
    # disponibles immédiatement, en un seul calcul.
    st.session_state.sim_clock_min = 10**9
    sim_time = int(st.session_state.sim_clock_min)

elif is_dynamic_no_tracking:
    st.sidebar.markdown("---")
    st.sidebar.header("📍 Horloge de simulation")
    st.sidebar.caption(
        "L'horloge pilote uniquement l'apparition des commandes/événements dans le "
        "temps — aucune position GPS n'est simulée, aucun camion n'est animé sur la carte."
    )
    if not st.session_state.simulation_started:
        if st.sidebar.button("🚀 Démarrer la simulation", type="primary"):
            st.session_state.simulation_started = True
        st.sidebar.caption("⏸️ Horloge à 0 min — cliquez pour démarrer.")
    else:
        st.sidebar.success("▶️ Simulation démarrée")

    col_track1, col_track2 = st.sidebar.columns(2)
    manual_advance_clicked = col_track1.button("➡️ +10 min simulées") and st.session_state.simulation_started
    reset_clicked = col_track2.button("🔄 Réinitialiser l'horloge")
    if reset_clicked:
        st.session_state.sim_clock_min = 0.0
        st.session_state.simulation_started = False
        st.session_state.delivered_ids = set()
        st.session_state.traveled_km_checkpoint = 0.0
    if manual_advance_clicked:
        st.session_state.sim_clock_min = min(1440.0, st.session_state.sim_clock_min + 10.0)

    if st.sidebar.button("⏹️ Arrêter la simulation") and st.session_state.simulation_started:
        st.session_state.simulation_started = False
        st.rerun()

    sim_time = int(st.session_state.sim_clock_min)

else:  # is_tracking — comportement complet d'origine
    st.sidebar.markdown("---")
    st.sidebar.header("📍 Horloge de simulation & tracking des camions")
    st.sidebar.caption(
        "Une seule horloge de simulation pilote à la fois l'apparition des commandes et le "
        "déplacement réel des camions (vitesse moyenne, sur le tracé réel de chaque tournée)."
    )
    truck_speed_kmh = st.sidebar.slider(
        "Vitesse moyenne des camions (km/h)", 15, 60, 30,
        help="Vitesse commerciale en zone urbaine (arrêts, feux, trafic inclus). "
             "Détermine le temps réel nécessaire pour parcourir chaque tournée.",
    )
    time_accel_options = {
        "Temps réel (1h simulée = 60 min réelles)": 60,
        "Rapide (1h simulée = 10 min réelles)": 10,
        "Très rapide (1h simulée = 5 min réelles)": 5,
        "Ultra rapide (1h simulée = 1 min réelle)": 1,
    }
    time_accel_label = st.sidebar.selectbox(
        "Vitesse d'accélération du temps", list(time_accel_options.keys()), index=2,
    )
    real_minutes_per_sim_hour = time_accel_options[time_accel_label]
    sim_minutes_per_real_second = 60 / (real_minutes_per_sim_hour * 60)

    if not st.session_state.simulation_started:
        if st.sidebar.button("🚀 Démarrer la simulation", type="primary"):
            st.session_state.simulation_started = True
        st.sidebar.caption("⏸️ Horloge à 0 min — cliquez pour démarrer la tournée.")
    else:
        st.sidebar.success("▶️ Simulation démarrée")

    col_track1, col_track2 = st.sidebar.columns(2)
    manual_advance_clicked = col_track1.button("➡️ +10 min simulées") and st.session_state.simulation_started
    reset_clicked = col_track2.button("🔄 Réinitialiser l'horloge")
    if reset_clicked:
        st.session_state.sim_clock_min = 0.0
        st.session_state.simulation_started = False
        # CORRECTIF (compteurs "commandes livrées" / "distance parcourue" erronés) :
        # un vrai redémarrage de l'horloge doit aussi remettre à zéro l'historique des
        # livraisons et le checkpoint de distance — sinon les commandes de la simulation
        # précédente restaient "livrées" pour toujours (donc jamais re-routées) et le
        # compteur de distance repartait avec l'ancien total au lieu de 0.
        st.session_state.delivered_ids = set()
        st.session_state.traveled_km_checkpoint = 0.0
    if manual_advance_clicked:
        st.session_state.sim_clock_min = min(1440.0, st.session_state.sim_clock_min + 10.0)

    auto_run = st.sidebar.toggle("▶️ Simulation temps réel (auto-refresh)", key="auto_run_active")
    if st.sidebar.button("⏹️ Arrêter la simulation") and st.session_state.simulation_started:
        st.session_state.stop_requested = True  # coupera auto_run_active au tout début du prochain run
        st.session_state.simulation_started = False  # fait réapparaître le bouton "🚀 Démarrer"
        st.rerun()
    auto_run = auto_run and st.session_state.simulation_started

    sim_time = int(st.session_state.sim_clock_min)

# État ACTUEL des paramètres : recalculé à CHAQUE exécution du script (Streamlit relance
# tout le script à chaque interaction — changement de slider, de sélecteur, événement
# appliqué, etc.), donc ces valeurs reflètent toujours la configuration en vigueur au
# moment présent plutôt qu'un instantané figé du tout premier chargement.
current_orders_df = (
    pd.concat([df_orders, pd.DataFrame(st.session_state.extra_orders, columns=COLUMNS)], ignore_index=True)
    if st.session_state.extra_orders else df_orders
)
current_active_orders_df = current_orders_df[~current_orders_df["id"].isin(st.session_state.cancelled_ids)]
current_effective_vehicles = max(1, num_vehicles - st.session_state.vehicle_breakdown_count)

with st.expander("📋 État actuel (paramètres en vigueur)", expanded=False):
    c1, c2, c3 = st.columns(3)
    c1.metric("Jeu de données", dataset_choice.split(" :")[0])
    c1.metric("Camions disponibles", f"{current_effective_vehicles} / {num_vehicles}")
    c2.metric("Capacité / camion", f"{vehicle_capacity} kg")
    if is_tracking:
        c2.metric("Vitesse moyenne", f"{truck_speed_kmh} km/h")
    c3.metric("Commandes actives", len(current_active_orders_df))
    c3.metric("Demande totale actuelle", f"{int(current_active_orders_df['demand_kg'].sum())} kg")
    st.caption(
        (f"Accélération du temps : {time_accel_label} · Horloge simulée : "
         f"{int(st.session_state.sim_clock_min)} min"
         if not is_static else "Calcul statique — pas d'horloge de simulation")
        + (f" · 🚧 {st.session_state.vehicle_breakdown_count} véhicule(s) en panne"
           if st.session_state.vehicle_breakdown_count else "")
        + (f" · 🚦 trafic x{st.session_state.traffic_penalty:.1f}"
           if st.session_state.traffic_penalty > 1.0 else "")
    )

manual_cancel_target = None

# ----------------------------------------------------------------------------
# 4. BARRE LATÉRALE — ÉVÉNEMENTS DYNAMIQUES (désormais réellement actifs)
#    Absente en mode statique : un calcul unique n'a, par définition, pas
#    d'événements qui surviennent « pendant » la tournée.
# ----------------------------------------------------------------------------
if not is_static:
    st.sidebar.markdown("---")
    st.sidebar.header("🚨 Événements dynamiques")
    st.sidebar.caption("Chaque événement modifie réellement les données et relance l'optimisation.")

    manual_event_type = st.sidebar.selectbox(
        "Déclencher un événement",
        ["AUCUN", "NOUVELLE_COMMANDE", "ANNULATION_COMMANDE", "EMBOUTEILLAGE", "ROUTE_FERMEE",
         "PANNE_VEHICULE", "ALERTE_TEMPERATURE", "SORTIE_ZONE", "CLIENT_ABSENT"],
    )

    if manual_event_type == "ANNULATION_COMMANDE":
        known_ids_now = pd.concat(
            [df_orders["id"], pd.Series([o["id"] for o in st.session_state.extra_orders], dtype=str)]
        )
        # Seules les commandes pas encore livrées (ni déjà annulées) peuvent être choisies —
        # "delivered_ids" est recalculé soit à partir de la progression réelle des camions
        # (mode tracking), soit à partir des validations manuelles (mode sans tracking).
        cancellable_ids = [
            i for i in known_ids_now
            if i not in st.session_state.cancelled_ids and i not in st.session_state.delivered_ids
        ]
        if cancellable_ids:
            manual_cancel_target = st.sidebar.selectbox(
                "Commande à annuler (non encore livrée)", cancellable_ids,
            )
        else:
            st.sidebar.caption("Aucune commande annulable : toutes livrées ou déjà annulées.")

    if st.sidebar.button("⚠️ Appliquer l'événement") and manual_event_type != "AUCUN":
        decision = process_dynamic_event(manual_event_type, {"vehicle_id": "V1"})
        known_ids = pd.concat(
            [df_orders["id"], pd.Series([o["id"] for o in st.session_state.extra_orders], dtype=str)]
        )
        candidate_ids = [i for i in known_ids if i not in st.session_state.cancelled_ids]
        detail = apply_event_effect(
            manual_event_type, st.session_state, depot_coords, sim_time, candidate_ids,
            manual_target=manual_cancel_target,
        )
        message = decision["message"] + (f" — {detail}" if detail else "")
        log_event(f"{manual_event_type} → {decision['action']} ({message})")
        st.rerun()

    if st.sidebar.button("🔄 Réinitialiser les événements"):
        st.session_state.extra_orders = []
        st.session_state.cancelled_ids = set()
        st.session_state.priority_overrides = {}
        st.session_state.vehicle_breakdown_count = 0
        st.session_state.traffic_penalty = 1.0
        log_event("Réinitialisation manuelle des événements dynamiques.")
        st.rerun()

    active_effects = []
    if st.session_state.vehicle_breakdown_count:
        active_effects.append(f"🚧 {st.session_state.vehicle_breakdown_count} véhicule(s) en panne")
    if st.session_state.traffic_penalty > 1.0:
        active_effects.append(f"🚦 trafic dégradé (x{st.session_state.traffic_penalty:.1f})")
    if st.session_state.cancelled_ids:
        active_effects.append(f"❌ {len(st.session_state.cancelled_ids)} commande(s) retirée(s)")
    if st.session_state.extra_orders:
        active_effects.append(f"🆕 {len(st.session_state.extra_orders)} commande(s) ajoutée(s)")
    if active_effects:
        st.sidebar.caption(" · ".join(active_effects))

# ----------------------------------------------------------------------------
# 4bis. BARRE LATÉRALE — LIVRAISON MANUELLE (mode dynamique SANS tracking)
#    Sans position GPS simulée, la livraison d'une commande ne peut pas être
#    détectée automatiquement (elle l'est normalement via la distance parcourue
#    par le camion, voir dvrp_map_component). On l'acte donc manuellement.
# ----------------------------------------------------------------------------
if is_dynamic_no_tracking and st.session_state.simulation_started:
    current_orders_now = (
        pd.concat([df_orders, pd.DataFrame(st.session_state.extra_orders, columns=COLUMNS)], ignore_index=True)
        if st.session_state.extra_orders else df_orders
    )
    routed_now_ids = current_orders_now[
        (~current_orders_now["id"].isin(st.session_state.cancelled_ids))
        & (~current_orders_now["id"].isin(st.session_state.delivered_ids))
        & (current_orders_now["release_time"] <= sim_time)
    ]["id"].tolist()
    st.sidebar.markdown("---")
    st.sidebar.header("✅ Livraison (sans tracking)")
    st.sidebar.caption(
        f"{len(routed_now_ids)} commande(s) actuellement en tournée. Sans position GPS, "
        "validez leur livraison manuellement pour faire progresser la simulation."
    )
    if st.sidebar.button("✅ Marquer la tournée courante comme livrée") and routed_now_ids:
        st.session_state.delivered_ids |= set(routed_now_ids)
        log_event(f"{len(routed_now_ids)} commande(s) marquée(s) livrée(s) manuellement (sans tracking).")
        st.rerun()

# ----------------------------------------------------------------------------
# 5. BARRE LATÉRALE — MQTT (télémétrie temps réel)
#    Réservée au mode « avec tracking » : c'est littéralement le flux de
#    position GPS temps réel, absent des deux autres modes.
# ----------------------------------------------------------------------------
if is_tracking:
    st.sidebar.markdown("---")
    st.sidebar.header("📡 MQTT (télémétrie temps réel)")
    st.sidebar.caption("Broker public de démonstration — non sécurisé, à usage pédagogique uniquement.")
    mqtt_enabled = st.sidebar.checkbox("Activer la connexion MQTT", value=False)

    if mqtt_enabled and st.session_state.mqtt_bridge is None:
        bridge = MQTTBridge()
        if bridge.start():
            st.session_state.mqtt_bridge = bridge
            st.sidebar.success("Connecté au broker MQTT.")
        else:
            st.sidebar.error("Connexion MQTT impossible (réseau indisponible).")
    elif not mqtt_enabled and st.session_state.mqtt_bridge is not None:
        st.session_state.mqtt_bridge.stop()
        st.session_state.mqtt_bridge = None

    if st.session_state.mqtt_bridge is not None:
        # On vide la queue thread-safe UNIQUEMENT ici, dans le thread principal Streamlit.
        new_events = st.session_state.mqtt_bridge.drain_events()
        st.session_state.mqtt_events.extend(new_events)
        st.session_state.mqtt_events = st.session_state.mqtt_events[-50:]

        st.sidebar.subheader("Envoyer une commande")
        target_v = st.sidebar.selectbox("Véhicule cible", [f"V{i + 1}" for i in range(num_vehicles)])
        cmd_type = st.sidebar.selectbox("Commande", ["MODIFIER_TOURNEE", "ARRETER_ALERTE", "RECALCULER_ITINERAIRE"])
        if st.sidebar.button("Envoyer la commande MQTT"):
            st.session_state.mqtt_bridge.send_command(target_v, {"command": cmd_type, "timestamp": time.time()})
            st.sidebar.info(f"Commande {cmd_type} envoyée à {target_v}")

st.sidebar.markdown("---")

# ----------------------------------------------------------------------------
# 6-9. CALCUL DVRP, CARTE & TABLEAU DE BORD — tout est délégué au composant React
#      (dvrp_map_component) une fois les tournées calculées : Python ne resynchronise
#      PAS périodiquement pendant la simulation. L'horloge, le déplacement des camions,
#      l'apparition des commandes, les KPI et le tableau vivent entièrement côté
#      navigateur. Cette fonction n'est donc appelée qu'une fois par rerun Streamlit —
#      qui n'a lieu que sur une action réelle (bouton, changement de paramètre,
#      événement, ou un retour du composant quand une commande est livrée).
# ----------------------------------------------------------------------------
def render_simulation():
    if not st.session_state.simulation_started:
        st.info(
            "🚀 Cliquez sur **« Démarrer la simulation »** dans la barre latérale pour "
            "calculer et afficher les tournées optimisées, la carte et le suivi des camions."
        )
        return

    if st.session_state.extra_orders:
        new_ids = {o["id"] for o in st.session_state.extra_orders}
        orders_df = pd.concat(
            [df_orders, pd.DataFrame(st.session_state.extra_orders, columns=COLUMNS)],
            ignore_index=True,
        )
    else:
        new_ids = set()
        orders_df = df_orders.copy()

    if st.session_state.priority_overrides:
        orders_df["priority"] = orders_df.apply(
            lambda r: st.session_state.priority_overrides.get(r["id"], r["priority"]), axis=1
        )

    # RÉOPTIMISATION DYNAMIQUE RÉELLE : seules les commandes déjà "arrivées" (release_time
    # <= horloge de simulation courante) ET pas encore livrées sont transmises au solveur.
    # Les commandes futures (release_time > horloge) ne sont PAS routées tant qu'elles ne
    # sont pas arrivées — elles apparaissent sur la carte à titre indicatif ("en attente")
    # et déclenchent, au moment précis de leur arrivée, un vrai recalcul OR-Tools (voir la
    # gestion de `new_order_arrived` plus bas). La séquence de tournée est donc recalculée
    # à chaque commande livrée ET à chaque nouvelle commande arrivée, pas une seule fois au
    # démarrage.
    non_cancelled_orders = orders_df[~orders_df["id"].isin(st.session_state.cancelled_ids)]
    delivered_ids = st.session_state.delivered_ids

    active_orders = non_cancelled_orders[
        (non_cancelled_orders["release_time"] <= sim_time)
        & (~non_cancelled_orders["id"].isin(delivered_ids))
    ].reset_index(drop=True)
    future_orders = non_cancelled_orders[
        non_cancelled_orders["release_time"] > sim_time
    ].reset_index(drop=True)
    delivered_orders = non_cancelled_orders[
        non_cancelled_orders["id"].isin(delivered_ids)
    ].reset_index(drop=True)

    if len(active_orders) == 0 and len(future_orders) == 0 and len(delivered_orders) == 0:
        st.info("Aucune commande disponible — vérifiez les événements appliqués.")
        return

    effective_vehicles = max(1, num_vehicles - st.session_state.vehicle_breakdown_count)
    if effective_vehicles < num_vehicles:
        st.warning(
            f"🚧 {st.session_state.vehicle_breakdown_count} camion(s) en panne : "
            f"{effective_vehicles}/{num_vehicles} véhicule(s) réellement disponibles."
        )

    # GARDE-FOU EXPLICITE : on retire une seconde fois, juste avant de construire les
    # données envoyées au solveur, toute commande déjà livrée. Le filtre plus haut
    # (ligne "active_orders = non_cancelled_orders[...]") suffit en théorie, mais cette
    # étape est délibérément dupliquée ICI, au point d'entrée réel d'OR-Tools, pour
    # qu'aucune commande livrée ne puisse JAMAIS lui être transmise — même si une future
    # modification venait, par erreur, à casser ou contourner le filtre initial.
    leaked_delivered = active_orders[active_orders["id"].isin(st.session_state.delivered_ids)]
    if not leaked_delivered.empty:
        # Ne devrait jamais arriver : on log l'anomalie plutôt que de planter, puis on
        # retire quand même ces commandes avant l'appel à OR-Tools.
        log_event(
            f"⚠️ Anomalie interne : {len(leaked_delivered)} commande(s) déjà livrée(s) "
            f"détectée(s) juste avant l'appel OR-Tools — retirées par sécurité."
        )
        active_orders = active_orders[
            ~active_orders["id"].isin(st.session_state.delivered_ids)
        ].reset_index(drop=True)

    total_demand = int(active_orders["demand_kg"].sum())

    if len(active_orders) > 0:
        coords_list = [depot_coords] + list(zip(active_orders["lat"], active_orders["lon"]))
        demands = [0] + active_orders["demand_kg"].astype(int).tolist()

        # Nombre de rotations par camion calculé automatiquement à partir de la demande totale
        # et de la capacité réellement disponible (plus besoin de régler un curseur manuel).
        # +1 rotation de marge : laisse au solveur une capacité légèrement excédentaire pour
        # répartir les tournées efficacement (sinon, avec le compte pile, il peut n'exister
        # aucune répartition valide même quand la capacité totale suffit tout juste).
        max_trips_per_vehicle = max(
            1, math.ceil(total_demand / (effective_vehicles * vehicle_capacity)) + 1
        )

        # Le solveur reçoit des véhicules "virtuels" (camion physique x trajets max autorisés),
        # tous de même capacité : ça lui permet de répartir une commande sur plusieurs rotations
        # d'un même camion si la capacité en un seul passage ne suffit pas.
        virtual_vehicle_count = effective_vehicles * max_trips_per_vehicle
        vehicle_capacities = [vehicle_capacity] * virtual_vehicle_count

        # ----------------------------------------------------------------------------
        # 7. CALCUL DVRP (OSRM + OR-Tools, avec capacité, rotations et pénalité de trafic)
        #    Relancé à chaque fois que l'ensemble des commandes "arrivées" change : une
        #    livraison qui vient de se terminer (commande retirée du problème) ou une
        #    nouvelle commande qui vient d'atteindre son release_time (commande ajoutée
        #    au problème) déclenchent chacune une VRAIE replanification OR-Tools ici.
        # ----------------------------------------------------------------------------
        with st.spinner("Calcul des distances et optimisation des tournées..."):
            raw_dist_matrix = get_osrm_distance_matrix(tuple(coords_list))  # distances réelles (km affichés)
            solver_dist_matrix = raw_dist_matrix
            if st.session_state.traffic_penalty > 1.0:
                # La pénalité de trafic influence UNIQUEMENT la décision d'OR-Tools (pour qu'il évite
                # la zone concernée) ; les distances affichées restent les vraies distances physiques.
                solver_dist_matrix = (np.array(raw_dist_matrix) * st.session_state.traffic_penalty).tolist()
            virtual_routes = solve_dvrp_ortools(solver_dist_matrix, demands, vehicle_capacities)

        total_capacity = vehicle_capacity * virtual_vehicle_count
        if not virtual_routes:
            st.error(
                f"⚠️ Aucune tournée réalisable : {total_demand} kg de commandes pour "
                f"{total_capacity} kg de capacité totale disponible ({effective_vehicles} véhicule(s) "
                f"x {max_trips_per_vehicle} rotation(s), calculées automatiquement). "
                f"Augmentez le nombre de véhicules ou leur capacité, ou réinitialisez les événements."
            )
            st.stop()

        # Regroupe les tournées virtuelles en rotations successives par camion physique.
        truck_trips = group_multi_trip_routes(virtual_routes, effective_vehicles)
        optimized_routes = [route for trips in truck_trips.values() for route in trips]  # pour les KPI globaux
        total_trips = len(optimized_routes)
        multi_trip_trucks = sum(1 for trips in truck_trips.values() if len(trips) > 1)

        # ----------------------------------------------------------------------------
        # 7bis. PREUVE DE L'OPTIMISATION : distance réelle vs référence non optimisée
        # ----------------------------------------------------------------------------
        # On mesure la distance physique (raw_dist_matrix, sans la pénalité de trafic qui ne sert
        # qu'à orienter le solveur) des tournées OR-Tools, et on la compare à une tournée « naïve »
        # qui affecte les commandes dans leur ordre d'apparition, sans aucune optimisation.
        optimized_distance_km = sum(route_distance(r, raw_dist_matrix) for r in optimized_routes) / 1000
        baseline_routes = naive_baseline_routes(demands, vehicle_capacities)
        baseline_distance_km = sum(route_distance(r, raw_dist_matrix) for r in baseline_routes) / 1000
        gain_km = baseline_distance_km - optimized_distance_km
        gain_pct = (gain_km / baseline_distance_km * 100) if baseline_distance_km > 0 else 0
    else:
        # Rien à router pour l'instant : soit tout est déjà livré, soit on attend l'arrivée
        # de la prochaine commande (release_time pas encore atteint). On garde néanmoins
        # l'interface active (carte + horloge) pour que le composant React continue de
        # faire avancer le temps et puisse détecter l'arrivée de la prochaine commande,
        # ce qui déclenchera alors une vraie replanification (voir plus bas).
        coords_list = [depot_coords]
        raw_dist_matrix = [[0.0]]
        truck_trips = {}
        optimized_routes = []
        total_trips = 0
        multi_trip_trucks = 0
        virtual_vehicle_count = effective_vehicles
        total_capacity = vehicle_capacity * effective_vehicles
        optimized_distance_km = 0.0
        baseline_distance_km = 0.0
        gain_pct = 0.0

    # ----------------------------------------------------------------------------
    # 8. INDICATEURS STATIQUES (ne changent pas avec le temps simulé — donc Python
    #    peut les afficher une fois pour toutes, sans resynchronisation périodique)
    # ----------------------------------------------------------------------------
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("📦 Quantité en tournée", f"{total_demand} kg", delta=f"capacité totale {total_capacity} kg")
    k2.metric("🚛 Camions utilisés", f"{len(truck_trips)} / {num_vehicles}")
    k3.metric("🔁 Trajets prévus", f"{total_trips}",
              delta=f"{multi_trip_trucks} en rotation multiple" if multi_trip_trucks else None)
    k4.metric("📋 Commandes en cours", f"{len(active_orders)}")
    st.caption(
        f"🕓 En attente d'arrivée : {len(future_orders)} · ✅ Déjà livrées : {len(delivered_orders)} "
        f"· 🚚 En cours de tournée : {len(active_orders)}"
    )

    if multi_trip_trucks:
        st.info(
            f"🔁 {multi_trip_trucks} camion(s) doivent effectuer **plusieurs rotations** "
            f"(retour au dépôt puis nouveau départ) pour livrer toutes les commandes : "
            f"la capacité en un seul passage est insuffisante avec les paramètres actuels."
        )

    # La preuve d'optimisation (distance avant/après, gain) apparaît normalement dans
    # le panneau "🎉 Tournée terminée" du composant React, une fois la simulation
    # effectivement terminée (les camions ont fini de parcourir leur tracé GPS —
    # voir dvrp_map_component/frontend/src/index.jsx : kpi.allFinished).
    #
    # La preuve d'optimisation (distance avant/après, gain) apparaît dans le panneau
    # "🎉 Tournée terminée" du composant React (voir plus bas, argument
    # `instant_finish` de dvrp_map()) : affiché dès qu'il y a des commandes routées
    # en mode statique ou dynamique sans tracking (aucune position GPS n'est jamais
    # simulée dans ces deux cas, donc le plan est déjà définitif dès son calcul), et
    # une fois la simulation GPS effectivement terminée en mode avec tracking.

    if st.session_state.logs:
        st.info(f"Dernier événement : {st.session_state.logs[0]}")

    st.markdown("---")
    col_map, col_details = st.columns([2, 1])

    with col_map:
        st.subheader("🗺 Carte")

        route_colors = ["blue", "green", "purple", "orange", "darkred", "cadetblue"]

        # full_shapes[p] = tracé combiné de TOUTE la rotation du camion physique p (tous
        # trajets mis bout à bout) ; trip_shapes[p] = tracés séparés par trajet (polylines).
        full_shapes: dict[int, list[list[float]]] = {}
        trip_shapes: dict[int, list[list[list[float]]]] = {}

        for p_idx, trips in truck_trips.items():
            full_shapes[p_idx] = []
            trip_shapes[p_idx] = []
            for route in trips:
                trip_shape: list[list[float]] = []
                for i in range(len(route) - 1):
                    p1 = coords_list[route[i]]
                    p2 = coords_list[route[i + 1]]
                    path = get_osrm_route_shape(tuple(p1), tuple(p2))
                    trip_shape.extend(path)
                full_shapes[p_idx].extend(trip_shape)
                trip_shapes[p_idx].append(trip_shape)

        # Un camion par index physique (0 à effective_vehicles-1), y compris ceux sans
        # tournée assignée (immobiles au dépôt) — voir historique de cette décision plus haut.
        trucks_payload = []
        for p_idx in range(effective_vehicles):
            color = route_colors[p_idx % len(route_colors)]
            shape = full_shapes.get(p_idx, [])
            if not shape:
                trucks_payload.append({"label": f"V{p_idx + 1}", "color": color, "used": False})
                continue

            # `stops` : distance cumulée (km) parcourue par CE camion jusqu'à chaque arrêt,
            # dans l'ordre de sa rotation — c'est ce qui permet au composant React de
            # déterminer, à partir de la distance parcourue qu'il calcule lui-même, quelles
            # commandes sont livrées, sans aucune aide de Python pendant la simulation.
            stops = []
            cum_km = 0.0
            for route in truck_trips[p_idx]:
                for i in range(len(route) - 1):
                    cum_km += raw_dist_matrix[route[i]][route[i + 1]] / 1000
                    node = route[i + 1]
                    if node != 0:
                        stops.append({"order_id": active_orders.iloc[node - 1]["id"], "cum_km": cum_km})

            trucks_payload.append({
                "label": f"V{p_idx + 1}", "color": color, "used": True,
                "shape": shape, "trip_shapes": trip_shapes[p_idx],
                "stops": stops, "total_km": cum_km, "speed_kmh": truck_speed_kmh,
            })

        payload_cols = ["id", "client", "lat", "lon", "demand_kg", "temp_max", "time_window", "priority", "release_time"]

        # Commandes réellement routées (arrivées, pas encore livrées) : rattachées à un
        # arrêt d'un camion (via trucks_payload / stops ci-dessus).
        orders_payload = active_orders[payload_cols].to_dict("records")
        for o in orders_payload:
            o["is_new"] = o["id"] in new_ids
            o["pending"] = False
            o["delivered"] = False

        # Commandes futures (release_time pas encore atteint) : affichées à titre indicatif
        # dès leur apparition sur la carte (marqueur en pointillés), mais PAS routées — dès
        # que l'horloge atteint leur release_time, le composant React le signale à Python
        # (voir `new_order_arrived` plus bas), ce qui déclenche une vraie replanification.
        pending_payload = future_orders[payload_cols].to_dict("records")
        for o in pending_payload:
            o["is_new"] = False
            o["pending"] = True
            o["delivered"] = False

        # Commandes déjà livrées : conservées dans le tableau de bord (statut "Livrée"),
        # mais retirées du problème de routage pour toute replanification future.
        delivered_payload = delivered_orders[payload_cols].to_dict("records")
        for o in delivered_payload:
            o["is_new"] = False
            o["pending"] = False
            o["delivered"] = True

        orders_payload = orders_payload + pending_payload + delivered_payload

        cancelled_payload = orders_df[
            orders_df["id"].isin(st.session_state.cancelled_ids)
        ][["id", "client", "demand_kg", "priority"]].to_dict("records")

        # Vrai composant Streamlit en React (dvrp_map_component/) : reçoit toutes les
        # commandes connues (routées, en attente, livrées) et fait vivre la simulation
        # côté navigateur — horloge, déplacement des camions, apparition des commandes,
        # KPI, tableau. Deux types de retour déclenchent un vrai recalcul Python : une
        # commande livrée (liste des commandes annulables) et une commande qui vient
        # d'atteindre son release_time (`new_order_arrived`), qui force une replanification
        # OR-Tools immédiate au lieu d'attendre le prochain événement manuel.
        #
        # RÉSERVÉ AU MODE TRACKING : sans position GPS simulée (statique / dynamique
        # sans tracking), ce composant reste bloqué sur son affichage "temps réel en
        # pause" (horloge figée à 0, "En attente de télémétrie...") — peu lisible pour
        # un résultat qui est en réalité déjà définitif. Ces deux modes ont leur propre
        # rendu natif, ci-dessous (carte statique + résultats), pensé pour eux.
        if is_tracking:
            dvrp_map_kwargs = dict(
                sim_clock_start_min=float(sim_time),
                sim_minutes_per_real_second=sim_minutes_per_real_second,
                auto_run=auto_run,
                planned_distance_km=optimized_distance_km,
                baseline_distance_km=baseline_distance_km,
                gain_pct=gain_pct,
                num_vehicles_used=len(truck_trips),
                num_vehicles_total=num_vehicles,
                height=480,
                key="dvrp_map",
                # CORRECTIF (compteurs "commandes livrées" / "distance parcourue" erronés) :
                # on transmet l'historique fiable côté Python (jamais réinitialisé), pour
                # que le composant amorce ses compteurs avec le total réel depuis le début
                # de la simulation, au lieu de repartir de zéro à chaque recalcul OR-Tools
                # (nouvelle commande, livraison, panne, etc. — voir index.jsx).
                already_delivered_ids=list(st.session_state.delivered_ids),
                already_traveled_km=st.session_state.traveled_km_checkpoint,
            )
            result = dvrp_map(depot_coords, orders_payload, cancelled_payload, trucks_payload, **dvrp_map_kwargs)
            if result:
                # Persiste le kilométrage cumulé rapporté par le composant : il sert de
                # nouveau point de départ ("checkpoint") au prochain recalcul OR-Tools, pour
                # que "distance parcourue" ne redescende jamais et reflète le vrai total.
                reported_km = result.get("distance_parcourue_km")
                if reported_km is not None:
                    st.session_state.traveled_km_checkpoint = float(reported_km)

                # CORRECTIF (bug de résurrection des commandes livrées) : on FUSIONNE
                # (union, |=) au lieu d'ÉCRASER (=) l'ensemble des commandes livrées.
                #
                # Pourquoi c'est nécessaire : à CHAQUE appel à OR-Tools (nouvelle commande
                # arrivée, annulation, panne véhicule...), les tournées changent, donc la
                # `structuralKey` du composant React change, donc TOUT le state d'animation
                # React est réinitialisé (voir dvrp_map_component/frontend/src/index.jsx) —
                # chaque camion repart virtuellement à 0 km sur son nouveau plan. À la toute
                # première frame après ce reset, `deliveredIds` calculé côté navigateur est
                # donc temporairement VIDE (aucun camion n'a encore reparcouru de distance),
                # et cette valeur transitoire est immédiatement renvoyée à Python.
                #
                # Avec une simple affectation (`=`), cette valeur vide ÉCRASAIT
                # `st.session_state.delivered_ids` : les commandes déjà livrées perdaient
                # leur statut, étaient réintégrées dans `active_orders` (filtre plus haut)
                # et renvoyées à OR-Tools comme si elles n'avaient jamais été livrées —
                # provoquant une re-livraison logique (et potentiellement une boucle, un
                # nouvel appel OR-Tools réinitialisant à nouveau le state React, etc.).
                #
                # `delivered_ids` doit être un ensemble MONOTONE CROISSANT : une commande
                # livrée le reste, quel que soit le nombre de réoptimisations ultérieures.
                st.session_state.delivered_ids |= set(result.get("delivered_ids", []))

                # REPLANIFICATION DYNAMIQUE : le composant React signale qu'une commande vient
                # d'atteindre son release_time (elle "arrive" à l'instant `sim_clock`). On fait
                # avancer l'horloge Python jusqu'à cet instant (pour que le filtre release_time
                # <= horloge, plus haut, l'inclue désormais) puis on relance tout le script :
                # active_orders changera, donc un nouveau calcul OR-Tools aura lieu et la
                # séquence de l'itinéraire construite sera mise à jour en conséquence.
                new_arrival_id = result.get("new_order_arrived")
                if new_arrival_id:
                    arrival_clock = float(result.get("sim_clock", st.session_state.sim_clock_min))
                    st.session_state.sim_clock_min = max(st.session_state.sim_clock_min, arrival_clock)
                    log_event(f"🆕 Commande {new_arrival_id} arrivée → replanification de l'itinéraire.")
                    st.rerun()
                # Arrêt automatique de la simulation temps réel dès que toutes les livraisons
                # sont terminées (le composant React le signale via all_finished). On ne peut
                # pas modifier auto_run_active ici directement (le toggle est déjà instancié
                # dans CE run) : on passe par stop_requested, appliqué au tout début du
                # prochain run. Protégé par la vérification de auto_run_active pour ne
                # déclencher ce rerun qu'une seule fois (pas de boucle infinie une fois arrêté).
                elif result.get("all_finished") and st.session_state.auto_run_active:
                    st.session_state.stop_requested = True
                    log_event("⏹️ Simulation arrêtée automatiquement — toutes les livraisons sont terminées.")
                    st.rerun()

        else:
            # ------------------------------------------------------------------------
            # RENDU NATIF (statique / dynamique sans tracking) : carte pydeck STATIQUE
            # (aucune animation, aucune dépendance au composant React) + résultats
            # clairement présentés (preuve d'optimisation, statuts des commandes).
            # ------------------------------------------------------------------------
            if len(active_orders) > 0:
                r1, r2, r3 = st.columns(3)
                r1.metric("📏 Distance planifiée", f"{optimized_distance_km:.1f} km",
                          delta=f"-{gain_pct:.0f} % vs non optimisé", delta_color="normal")
                r2.metric("📉 Distance sans optimisation", f"{baseline_distance_km:.1f} km")
                r3.metric("💰 Gain apporté par OR-Tools", f"{gain_pct:.0f} %")
            elif len(future_orders) > 0:
                st.info(f"⏳ En attente de l'arrivée de {len(future_orders)} commande(s) pour la prochaine tournée.")
            else:
                st.success("✅ Toutes les commandes connues ont été traitées.")

            ROUTE_COLORS_RGB = {
                "blue": [31, 119, 180], "green": [44, 160, 44], "purple": [148, 103, 189],
                "orange": [255, 127, 14], "darkred": [140, 0, 0], "cadetblue": [95, 158, 160],
            }
            # Couleur du camion assigné à chaque commande routée (via ses arrêts) — permet
            # de colorer les marqueurs de commandes de la même couleur que leur tournée.
            order_color_by_id: dict[str, list[int]] = {}
            for t in trucks_payload:
                if not t.get("used"):
                    continue
                rgb = ROUTE_COLORS_RGB.get(t["color"], [70, 70, 70])
                for stop in t.get("stops", []):
                    order_color_by_id[stop["order_id"]] = rgb

            path_data = [
                {"path": [[lon, lat] for lat, lon in t["shape"]],
                 "color": ROUTE_COLORS_RGB.get(t["color"], [70, 70, 70]), "label": t["label"]}
                for t in trucks_payload if t.get("used")
            ]
            order_points = []
            for o in orders_payload:
                if o.get("delivered"):
                    color, status = [34, 139, 34], "✅ Livrée"
                elif o.get("pending"):
                    color, status = [170, 170, 170], "🕓 En attente"
                else:
                    color, status = order_color_by_id.get(o["id"], [70, 70, 70]), "🚚 En tournée"
                order_points.append({
                    "lon": o["lon"], "lat": o["lat"], "color": color,
                    "name": o["client"], "id": o["id"], "kg": o["demand_kg"], "status": status,
                })
            depot_point = [{"lon": depot_coords[1], "lat": depot_coords[0], "name": "🏭 Dépôt", "status": "", "kg": ""}]

            if path_data or order_points:
                layers = [
                    pdk.Layer("PathLayer", data=path_data, get_path="path", get_color="color",
                               width_min_pixels=4, pickable=False),
                    pdk.Layer("ScatterplotLayer", data=order_points, get_position=["lon", "lat"],
                               get_fill_color="color", get_radius=70, radius_min_pixels=5,
                               radius_max_pixels=13, pickable=True, stroked=True,
                               get_line_color=[255, 255, 255], line_width_min_pixels=1),
                    pdk.Layer("ScatterplotLayer", data=depot_point, get_position=["lon", "lat"],
                               get_fill_color=[20, 20, 20], get_radius=160, radius_min_pixels=9,
                               pickable=True, stroked=True, get_line_color=[255, 255, 255],
                               line_width_min_pixels=2),
                ]
                st.pydeck_chart(
                    pdk.Deck(
                        layers=layers,
                        initial_view_state=pdk.ViewState(
                            latitude=depot_coords[0], longitude=depot_coords[1], zoom=11, pitch=0,
                        ),
                        tooltip={"text": "{name}\n{status} {kg}"},
                    ),
                    height=480,
                )
                st.caption(
                    "⬛ Dépôt · 🟢 Livrée · ⚪ En attente · 🔵 En tournée (couleur = camion assigné) "
                    "— carte statique, sans animation."
                )
            else:
                st.info("Aucune commande à afficher sur la carte pour l'instant.")

        if is_tracking:
            st.caption(
                f"🕒 Accélération : {time_accel_label.lower()} · 🚚 Vitesse moyenne assumée : "
                f"{truck_speed_kmh} km/h — le temps de trajet de chaque camion est calculé à "
                f"partir de sa distance réelle et de cette vitesse."
            )
        elif is_dynamic_no_tracking:
            st.caption(
                "🔄 Utilisez « ✅ Marquer la tournée courante comme livrée » (barre latérale) "
                "pour faire progresser la simulation vers la prochaine tournée."
            )

    with col_details:
        st.subheader("🗺️ Séquence de l'itinéraire construite")
        if is_static:
            st.caption("Calculée une seule fois à partir de l'ensemble des commandes connues.")
        else:
            st.caption(
                "Recalculée automatiquement (nouvel appel OR-Tools) à chaque commande livrée "
                "et à chaque nouvelle commande qui arrive à son heure — pas seulement lors des "
                "événements déclenchés manuellement."
            )
        if not truck_trips:
            if len(future_orders) > 0:
                st.caption(
                    f"⏳ Aucun trajet actif pour l'instant — en attente de l'arrivée de "
                    f"{len(future_orders)} commande(s) (replanification automatique dès arrivée)."
                )
            else:
                st.caption("✅ Toutes les commandes connues ont déjà été livrées.")
        for p_idx, trips in truck_trips.items():
            truck_total_km = sum(route_distance(r, raw_dist_matrix) for r in trips) / 1000
            truck_total_load = sum(
                active_orders.iloc[node - 1]["demand_kg"] for r in trips for node in r if node != 0
            )
            header = f"**Camion V{p_idx + 1}**"
            if len(trips) > 1:
                header += f" — 🔁 {len(trips)} rotations — {truck_total_load} kg au total — {truck_total_km:.1f} km"
            else:
                header += f" — {truck_total_load}/{vehicle_capacity} kg — {truck_total_km:.1f} km"
            st.markdown(header)
            for t_idx, route in enumerate(trips):
                stops_names = [active_orders.iloc[node - 1]["client"] for node in route if node != 0]
                load = sum(active_orders.iloc[node - 1]["demand_kg"] for node in route if node != 0)
                trip_km = route_distance(route, raw_dist_matrix) / 1000
                prefix = f"Trajet {t_idx + 1}/{len(trips)} " if len(trips) > 1 else ""
                st.caption(
                    f"{prefix}({load}/{vehicle_capacity} kg, {trip_km:.1f} km) : "
                    + " → ".join(["Dépôt"] + stops_names + ["Dépôt"])
                )

        if not is_tracking:
            st.subheader("📋 Commandes")
            table_rows = []
            for o in orders_payload:
                status = "✅ Livrée" if o.get("delivered") else ("🕓 En attente" if o.get("pending") else "🚚 En tournée")
                table_rows.append({
                    "ID": o["id"], "Client": o["client"], "Kg": o["demand_kg"],
                    "Priorité": o["priority"], "Statut": status,
                })
            for o in cancelled_payload:
                table_rows.append({
                    "ID": o["id"], "Client": o["client"], "Kg": o["demand_kg"],
                    "Priorité": o["priority"], "Statut": "❌ Annulée",
                })
            if table_rows:
                st.dataframe(
                    pd.DataFrame(table_rows), hide_index=True, width="stretch", height=220,
                )
            else:
                st.caption("Aucune commande connue pour l'instant.")

        if is_tracking:
            st.subheader("📡 Télémétrie MQTT")
            if st.session_state.mqtt_bridge is None:
                st.caption("MQTT désactivé — cochez la case dans la barre latérale pour l'activer.")
            elif st.session_state.mqtt_events:
                for event in reversed(st.session_state.mqtt_events[-5:]):
                    st.json(event, expanded=False)
            else:
                st.caption("En attente de télémétrie sur `fleet/telemetry/+`...")

        st.subheader("📋 Journal des évènements")
        st.text_area("Historique", value="\n".join(st.session_state.logs[:10]), height=150, label_visibility="collapsed")


render_simulation()
