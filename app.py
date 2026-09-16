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
st.title("🚚 Tracking en temps réel pour l'optimisation logistique : cas les tournées dynamiques DVRP")
st.caption("Alger — livraison de produits frais / express")
st.markdown("---")

# ----------------------------------------------------------------------------
# 3. BARRE LATÉRALE — DONNÉES
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# 2bis. ONGLETS : simulation en direct (comportement d'origine, inchangé) vs
#       comparatif pédagogique statique / dynamique sans tracking / tracking.
# ----------------------------------------------------------------------------
main_tab, compare_tab = st.tabs(["🚚 Simulation en direct", "📊 Comparatif Statique / Dynamique / Tracking"])

with main_tab:
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
        c2.metric("Vitesse moyenne", f"{truck_speed_kmh} km/h")
        c3.metric("Commandes actives", len(current_active_orders_df))
        c3.metric("Demande totale actuelle", f"{int(current_active_orders_df['demand_kg'].sum())} kg")
        st.caption(
            f"Accélération du temps : {time_accel_label} · Horloge simulée : "
            f"{int(st.session_state.sim_clock_min)} min"
            + (f" · 🚧 {st.session_state.vehicle_breakdown_count} véhicule(s) en panne"
               if st.session_state.vehicle_breakdown_count else "")
            + (f" · 🚦 trafic x{st.session_state.traffic_penalty:.1f}"
               if st.session_state.traffic_penalty > 1.0 else "")
        )

    # ----------------------------------------------------------------------------
    # 4. BARRE LATÉRALE — ÉVÉNEMENTS DYNAMIQUES (désormais réellement actifs)
    # ----------------------------------------------------------------------------
    st.sidebar.markdown("---")
    st.sidebar.header("🚨 Événements dynamiques")
    st.sidebar.caption("Chaque événement modifie réellement les données et relance l'optimisation.")

    manual_event_type = st.sidebar.selectbox(
        "Déclencher un événement",
        ["AUCUN", "NOUVELLE_COMMANDE", "ANNULATION_COMMANDE", "EMBOUTEILLAGE", "ROUTE_FERMEE",
         "PANNE_VEHICULE", "ALERTE_TEMPERATURE", "SORTIE_ZONE", "CLIENT_ABSENT"],
    )

    manual_cancel_target = None
    if manual_event_type == "ANNULATION_COMMANDE":
        known_ids_now = pd.concat(
            [df_orders["id"], pd.Series([o["id"] for o in st.session_state.extra_orders], dtype=str)]
        )
        # Seules les commandes pas encore livrées (ni déjà annulées) peuvent être choisies —
        # "delivered_ids" est recalculé à chaque affichage de la carte à partir de la
        # progression réelle des camions sur leur tournée.
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
    # 5. BARRE LATÉRALE — MQTT (télémétrie temps réel)
    # ----------------------------------------------------------------------------
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

        # La preuve d'optimisation (distance avant/après, gain) n'est plus affichée ici de
        # façon statique — elle apparaît désormais dans le panneau "🎉 Tournée terminée" du
        # composant React, une fois la simulation effectivement terminée (voir plus bas :
        # planned_distance_km / baseline_distance_km / gain_pct transmis à dvrp_map()).

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
            result = dvrp_map(
                depot_coords, orders_payload, cancelled_payload, trucks_payload,
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

            st.caption(
                f"🕒 Accélération : {time_accel_label.lower()} · 🚚 Vitesse moyenne assumée : "
                f"{truck_speed_kmh} km/h — le temps de trajet de chaque camion est calculé à "
                f"partir de sa distance réelle et de cette vitesse."
            )

        with col_details:
            st.subheader("🗺️ Séquence de l'itinéraire construite")
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


# ==============================================================================
# 📊 ONGLET COMPARATIF — démontrer la valeur ajoutée du dynamisme + du tracking
# ==============================================================================
# Principe : rejouer EXACTEMENT le même scénario (mêmes commandes, mêmes imprévus,
# aux mêmes instants) sous 3 niveaux d'information différents, puis comparer ce que
# chaque mode a réellement pu prendre en compte dans le plan qu'il exécute :
#   - 🧊 Statique : calcule une fois à t=0 et n'observe plus jamais rien ensuite.
#   - 🔄 Sans tracking : ne "voit" les événements qu'à des points de contrôle
#     espacés (ex. toutes les 25 min) — d'où un délai de réaction.
#   - 📡 Avec tracking : réagit quasi immédiatement à chaque événement (délai =
#     temps de calcul OR-Tools, de l'ordre de la minute).
# Les distances affichées sont de VRAIS calculs OR-Tools sur des sous-ensembles
# réels du jeu de données choisi dans la barre latérale — seul le déroulé temporel
# des événements (instants, fréquence des points de contrôle) est un scénario
# pédagogique simplifié et déterministe (graine fixe), assumé et expliqué comme tel.
def _comparative_scenario_events(df_orders_full: pd.DataFrame, seed: int = 42):
    """Scénario déterministe : ~30 % des commandes n'arrivent qu'en cours de route,
    un camion tombe en panne, 2 commandes sont annulées — toujours les mêmes (graine
    fixe), pour que la comparaison soit reproductible et équitable entre les 3 modes."""
    rng = np.random.default_rng(seed)
    order_ids = df_orders_full["id"].tolist()
    n = len(order_ids)
    shuffled = list(order_ids)
    rng.shuffle(shuffled)
    split = max(1, int(n * 0.7)) if n > 1 else n

    initial_ids = set(shuffled[:split])
    new_order_ids = set(shuffled[split:])
    cancel_pool = shuffled[:split]
    cancel_n = min(2, len(cancel_pool))
    cancelled_ids = set(rng.choice(cancel_pool, size=cancel_n, replace=False)) if cancel_n else set()

    events = [{"t": 30, "type": "PANNE_VEHICULE", "label": "🚧 Panne d'un camion"}]
    if new_order_ids:
        events.insert(0, {"t": 10, "type": "NOUVELLE_COMMANDE", "order_ids": new_order_ids,
                           "label": f"🆕 {len(new_order_ids)} nouvelle(s) commande(s)"})
    if cancelled_ids:
        events.append({"t": 55, "type": "ANNULATION_COMMANDE", "order_ids": cancelled_ids,
                        "label": f"❌ {len(cancelled_ids)} annulation(s)"})
    return initial_ids, new_order_ids, cancelled_ids, events


def _solve_plan_km(df_subset: pd.DataFrame, depot, vehicles_available: int, capacity: int):
    """Résolution OR-Tools réelle (avec rotations si la capacité l'exige) sur un
    sous-ensemble de commandes. Retourne (distance_km, nb_trajets) ou (None, 0) si
    aucune solution (capacité totale insuffisante)."""
    if len(df_subset) == 0:
        return 0.0, 0
    coords_list = [depot] + list(zip(df_subset["lat"], df_subset["lon"]))
    demands = [0] + df_subset["demand_kg"].astype(int).tolist()
    total_demand = sum(demands)
    vehicles_available = max(1, vehicles_available)
    max_trips = max(1, math.ceil(total_demand / (vehicles_available * capacity)) + 1)
    capacities = [capacity] * (vehicles_available * max_trips)
    dist_matrix = get_osrm_distance_matrix(tuple(coords_list))
    routes = solve_dvrp_ortools(dist_matrix, demands, capacities, time_limit_s=3)
    if not routes:
        return None, 0
    used_routes = [r for r in routes if len(r) > 2]
    distance_km = sum(route_distance(r, dist_matrix) for r in used_routes) / 1000
    return distance_km, len(used_routes)


def render_comparative_tab():
    st.subheader("📊 Un même scénario, trois niveaux d'information")
    st.markdown(
        "Le **même jeu de commandes** (celui sélectionné dans la barre latérale) et le "
        "**même déroulé d'imprévus** — une partie des commandes n'arrive qu'en cours de "
        "route, un camion tombe en panne, deux commandes sont annulées — est rejoué dans "
        "les 3 modes. Seule change **la rapidité avec laquelle chaque mode en a "
        "connaissance**. Les distances sont de vrais calculs OR-Tools ; le déroulé des "
        "événements (instants, fréquence des points de contrôle) est un scénario "
        "pédagogique simplifié et déterministe, pour que la comparaison soit "
        "reproductible."
    )

    if st.button("▶️ Lancer la comparaison", type="primary"):
        st.session_state["_comp_run"] = True
        st.session_state["_comp_signature"] = None  # force un recalcul

    if not st.session_state.get("_comp_run"):
        st.info("Cliquez sur **« Lancer la comparaison »** pour exécuter le scénario dans les 3 modes.")
        return

    initial_ids, new_order_ids, cancelled_ids, events = _comparative_scenario_events(df_orders)
    initial_df = df_orders[df_orders["id"].isin(initial_ids)].reset_index(drop=True)

    # Recalcule uniquement si les paramètres pertinents (dataset, flotte, capacité) ont
    # changé depuis la dernière exécution — évite de relancer OR-Tools à chaque interaction
    # sans rapport (ex : ouvrir un expander) dans les autres onglets.
    signature = (dataset_choice, num_vehicles, vehicle_capacity, len(df_orders))
    if st.session_state.get("_comp_signature") != signature:
        with st.spinner("Résolution OR-Tools pour les 3 modes (quelques secondes)..."):
            # 🧊 STATIQUE : ne connaît QUE l'état initial, avec la flotte "nominale" (elle
            # ignore la panne, puisqu'elle n'observe plus jamais rien après t=0).
            static_dist, static_trips = _solve_plan_km(initial_df, depot_coords, num_vehicles, vehicle_capacity)

            # 🔄 SANS TRACKING : points de contrôle toutes les 25 min → capte la nouvelle
            # commande (survenue à t=10) au 1er contrôle (t=25, soit 15 min de retard), et
            # la panne (survenue à t=30) au 2e contrôle (t=50, soit 20 min de retard).
            # L'annulation (t=55) survient APRÈS le dernier contrôle observé dans la
            # fenêtre (t=50) : elle n'est donc pas encore prise en compte.
            no_track_df = pd.concat(
                [initial_df, df_orders[df_orders["id"].isin(new_order_ids)]], ignore_index=True
            )
            no_track_vehicles = max(1, num_vehicles - 1)
            no_track_dist, no_track_trips = _solve_plan_km(
                no_track_df, depot_coords, no_track_vehicles, vehicle_capacity
            )
            no_track_reaction_delays = [25 - 10, 50 - 30]
            no_track_events_seen = 1 + (1 if new_order_ids else 0)  # panne + nouvelle commande

            # 📡 AVEC TRACKING : réagit à chaque événement quasi immédiatement (délai =
            # temps de calcul OR-Tools, de l'ordre de la minute) — les 3 événements sont
            # donc déjà intégrés dans le plan exécuté au sein de la même fenêtre.
            tracking_df = pd.concat(
                [initial_df, df_orders[df_orders["id"].isin(new_order_ids)]], ignore_index=True
            )
            tracking_df = tracking_df[~tracking_df["id"].isin(cancelled_ids)]
            tracking_vehicles = max(1, num_vehicles - 1)
            tracking_dist, tracking_trips = _solve_plan_km(
                tracking_df, depot_coords, tracking_vehicles, vehicle_capacity
            )
            tracking_reaction_delays = [1] * len(events)

        st.session_state["_comp_signature"] = signature
        st.session_state["_comp_results"] = dict(
            static_dist=static_dist, static_trips=static_trips,
            no_track_dist=no_track_dist, no_track_trips=no_track_trips,
            no_track_reaction_delays=no_track_reaction_delays, no_track_events_seen=no_track_events_seen,
            tracking_dist=tracking_dist, tracking_trips=tracking_trips,
            tracking_reaction_delays=tracking_reaction_delays,
        )

    r = st.session_state["_comp_results"]

    def _fmt_km(v):
        return f"{v:.1f} km" if v is not None else "❌ aucune solution (capacité insuffisante)"

    st.markdown("### 🏁 Résultats — fenêtre d'observation de 60 minutes simulées")
    df_compare = pd.DataFrame([
        {
            "Mode": "🧊 Statique",
            "Distance du plan réellement exécuté": _fmt_km(r["static_dist"]),
            "Événements pris en compte": f"0 / {len(events)}",
            "Délai de réaction moyen": "∞ (jamais)",
            "Replanifications déclenchées": 1,
        },
        {
            "Mode": "🔄 Dynamique sans tracking",
            "Distance du plan réellement exécuté": _fmt_km(r["no_track_dist"]),
            "Événements pris en compte": f"{r['no_track_events_seen']} / {len(events)}",
            "Délai de réaction moyen": f"{np.mean(r['no_track_reaction_delays']):.0f} min",
            "Replanifications déclenchées": 2,
        },
        {
            "Mode": "📡 Dynamique avec tracking",
            "Distance du plan réellement exécuté": _fmt_km(r["tracking_dist"]),
            "Événements pris en compte": f"{len(events)} / {len(events)}",
            "Délai de réaction moyen": f"{np.mean(r['tracking_reaction_delays']):.0f} min",
            "Replanifications déclenchées": len(events),
        },
    ])
    st.dataframe(df_compare, hide_index=True, width="stretch")

    c1, c2, c3 = st.columns(3)
    c1.metric("📏 Distance — Statique", _fmt_km(r["static_dist"]))
    delta2 = (f"{r['no_track_dist'] - r['static_dist']:+.1f} km vs statique"
              if r["no_track_dist"] is not None and r["static_dist"] is not None else None)
    c2.metric("📏 Distance — Sans tracking", _fmt_km(r["no_track_dist"]), delta=delta2, delta_color="off")
    delta3 = (f"{r['tracking_dist'] - r['no_track_dist']:+.1f} km vs sans tracking"
              if r["tracking_dist"] is not None and r["no_track_dist"] is not None else None)
    c3.metric("📏 Distance — Avec tracking", _fmt_km(r["tracking_dist"]), delta=delta3, delta_color="off")
    st.caption(
        "La distance n'est pas directement comparable terme à terme (chaque mode route un "
        "ensemble de commandes différent, reflet de ce qu'il savait au moment d'agir) — "
        "l'indicateur clé est plutôt le nombre d'événements réellement pris en compte "
        "et le délai de réaction, ci-dessus."
    )

    st.markdown("### 🔍 Ce que chaque mode a raté (ou pas)")
    colA, colB, colC = st.columns(3)
    with colA:
        st.error("🧊 **Statique**")
        st.write(f"❌ {len(new_order_ids)} nouvelle(s) commande(s) jamais intégrée(s) au plan")
        if cancelled_ids:
            st.write(f"❌ {len(cancelled_ids)} commande(s) annulée(s) mais toujours dans la tournée")
        st.write("❌ Panne du camion ignorée — plan invalide en pratique")
    with colB:
        st.warning("🔄 **Sans tracking**")
        if new_order_ids:
            st.write(f"✅ Nouvelle(s) commande(s) intégrée(s), avec **{r['no_track_reaction_delays'][0]} min** de retard")
        st.write(f"✅ Panne détectée, avec **{r['no_track_reaction_delays'][1]} min** de retard")
        if cancelled_ids:
            st.write(f"❌ {len(cancelled_ids)} annulation(s) pas encore prise(s) en compte dans la fenêtre observée")
    with colC:
        st.success("📡 **Avec tracking**")
        if new_order_ids:
            st.write(f"✅ Nouvelle(s) commande(s) intégrée(s) quasi immédiatement (~1 min)")
        st.write("✅ Panne détectée et compensée en quelques secondes")
        if cancelled_ids:
            st.write(f"✅ {len(cancelled_ids)} annulation(s) déjà retirée(s) du plan")

    st.caption(
        "⚠️ Scénario pédagogique déterministe (graine fixe) construit à partir du jeu de "
        "données et des réglages de flotte actuellement choisis dans la barre latérale : "
        "changez-les puis relancez la comparaison pour voir son effet."
    )
    if st.button("🔄 Réinitialiser la comparaison"):
        st.session_state["_comp_run"] = False
        st.session_state["_comp_signature"] = None
        st.rerun()


with compare_tab:
    render_comparative_tab()
