import React, { useEffect, useRef, useState } from "react";
import ReactDOM from "react-dom/client";
import { Streamlit, withStreamlitConnection } from "streamlit-component-lib";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

const PRIORITY_COLOR = { URGENTE: "#e53935", HAUTE: "#fb8c00", NORMALE: "#1e88e5" };
const PRIORITY_LABEL = { URGENTE: "🔴 Urgente", HAUTE: "🟠 Haute", NORMALE: "🔵 Normale" };
// Rayon des marqueurs commande sur la carte : les priorités élevées sont plus visibles.
const PRIORITY_RADIUS = { URGENTE: 11, HAUTE: 9.5, NORMALE: 7 };
// Priorités considérées comme "à surveiller" pour le suivi urgent/haute importance.
const HIGH_PRIORITIES = new Set(["URGENTE", "HAUTE"]);

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371.0;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

function interpolate(shape, cumKm, targetKm) {
  if (!shape || shape.length === 0) return null;
  if (targetKm <= 0) return shape[0];
  const total = cumKm[cumKm.length - 1];
  if (targetKm >= total) return shape[shape.length - 1];
  for (let i = 1; i < cumKm.length; i++) {
    if (cumKm[i] >= targetKm) {
      const segLen = cumKm[i] - cumKm[i - 1];
      const ratio = segLen === 0 ? 0 : (targetKm - cumKm[i - 1]) / segLen;
      return [
        shape[i - 1][0] + (shape[i][0] - shape[i - 1][0]) * ratio,
        shape[i - 1][1] + (shape[i][1] - shape[i - 1][1]) * ratio,
      ];
    }
  }
  return shape[shape.length - 1];
}

// Horloge affichée au format HH:MM (24h, plafonné à un jour de simulation) — plus
// lisible et plus "conviviale" qu'un affichage brut en minutes.
function fmtClock(min) {
  const m = Math.max(0, Math.round(min));
  const h = Math.floor(m / 60) % 24;
  const mm = m % 60;
  return String(h).padStart(2, "0") + ":" + String(mm).padStart(2, "0");
}

/**
 * Composant Streamlit (React) : carte des tournées DVRP + tableau de bord complet,
 * TOUT calculé et affiché côté navigateur (horloge, position des camions, commandes
 * révélées au fil du temps, commandes livrées, KPI, tableau). Python ne fait AUCUNE
 * resynchronisation périodique pendant que la simulation tourne — le composant reçoit
 * une fois toutes les données nécessaires (itinéraires + toutes les commandes avec leur
 * `release_time`) et fait vivre la simulation lui-même.
 *
 * Un retour vers Python (`Streamlit.setComponentValue`) a lieu quand l'ensemble des
 * commandes livrées change, quand une NOUVELLE commande arrive (release_time atteint —
 * ce qui déclenche une réoptimisation OR-Tools côté Python, rendant la planification
 * réellement dynamique), quand la tournée se termine, ou environ toutes les 20 min
 * simulées (pour garder l'horloge Python à peu près à jour). Ce retour inclut aussi
 * l'horloge courante, pour éviter qu'un événement déclenché en cours de route ne
 * reparte d'une horloge Python périmée.
 */
function DvrpMap({ args }) {
  const {
    depot,
    orders = [],
    cancelled_orders: cancelledOrders = [],
    trucks = [],
    height = 520,
    sim_clock_start_min: simClockStartMin = 0,
    sim_minutes_per_real_second: simMinutesPerRealSecond = 0,
    auto_run: autoRun = false,
    planned_distance_km: plannedDistanceKm = 0,
    baseline_distance_km: baselineDistanceKm = 0,
    gain_pct: gainPct = 0,
    num_vehicles_used: numVehiclesUsed = 0,
    num_vehicles_total: numVehiclesTotal = 0,
    // CORRECTIF (compteurs "commandes livrées" / "distance parcourue" erronés) :
    // historique fiable transmis par Python (session_state, jamais réinitialisé),
    // utilisé pour AMORCER les compteurs de ce composant au lieu de repartir de
    // zéro à chaque remontage (voir plus bas pourquoi un remontage a lieu à
    // chaque recalcul OR-Tools, donc à chaque livraison ou nouvelle commande).
    already_delivered_ids: alreadyDeliveredIds = [],
    already_traveled_km: alreadyTraveledKm = 0,
  } = args;

  const wrapperRef = useRef(null);
  const mapContainerRef = useRef(null);
  const mapRef = useRef(null);
  const truckStateRef = useRef({});
  const orderMarkersRef = useRef({});
  // id de commande -> priorité, reconstruit à chaque changement structurel — utilisé
  // dans la boucle d'animation pour le suivi urgent/haute importance par tournée.
  const orderPriorityRef = useRef({});
  const lastReportedRef = useRef(null);
  // CORRECTIF (commandes "en attente" jamais routées) : `new_order_arrived` était déjà
  // documenté et lu côté Python (app.py) mais jamais réellement renvoyé par le
  // composant — Streamlit.setComponentValue ne l'incluait jamais. Résultat : dès
  // qu'une commande atteignait son release_time en cours de simulation temps réel,
  // elle apparaissait bien sur la carte (marqueur "en attente") mais l'horloge Python
  // (qui filtre les commandes actives) ne se resynchronisait jamais toute seule —
  // seul le bouton manuel "+10 min" la faisait avancer. La commande restait donc
  // "en attente" indéfiniment, jamais transmise à OR-Tools, jamais livrée : le
  // compteur "X livrées sur Y" se figeait dès qu'une commande à release_time tardif
  // existait. Ce ref mémorise les commandes déjà signalées "arrivées" à Python.
  const reportedVisibleIdsRef = useRef(new Set());
  // Référence "légère" pour l'horloge : mise à jour SANS reconstruire la carte (voir
  // plus bas). Sépare ce qui doit reconstruire visuellement la carte (itinéraires
  // réellement différents) de ce qui ne doit que recaler discrètement le point de
  // départ du calcul du temps (resynchronisation périodique avec Python, play/pause).
  const baselineRef = useRef({ simClockStartMin, autoRun, startTime: performance.now() });

  const [kpi, setKpi] = useState({
    simClockMin: simClockStartMin,
    visibleCount: 0,
    totalOrders: orders.length,
    // Amorcés avec l'historique reçu de Python plutôt que [] / 0 : sans ça, le
    // compteur "commandes livrées" et "distance parcourue" retombaient à leur
    // valeur du segment courant (depuis le DERNIER recalcul OR-Tools) à chaque
    // remontage du composant, au lieu de refléter le total depuis le début de
    // la simulation.
    deliveredIds: alreadyDeliveredIds,
    distanceParcourue: alreadyTraveledKm,
    allFinished: false,
    // Suivi urgent/haute importance : commandes prioritaires encore en attente de
    // livraison, et détail par camion (tournée) pour repérer d'un coup d'œil quelle
    // tournée transporte des commandes sensibles.
    highPriorityPending: [],
    truckPriorityCounts: {},
  });

  // Clé structurelle : ne change QUE si les itinéraires (tracés, camions utilisés,
  // vitesse) ou la liste des commandes changent réellement — jamais à cause de
  // l'horloge ou du play/pause, qui sont gérés séparément ci-dessous sans jamais
  // reconstruire la carte (ce qui causait un clignotement toutes les ~20 min
  // simulées, à chaque resynchronisation ponctuelle de l'horloge avec Python).
  const structuralKey = JSON.stringify({
    depot,
    orders: orders.map((o) => o.id),
    trucks: trucks.map((t) => ({
      label: t.label, used: t.used, shape: t.shape, trip_shapes: t.trip_shapes, speed_kmh: t.speed_kmh,
    })),
    simMinutesPerRealSecond,
  });

  // Met à jour la référence d'horloge SANS reconstruire la carte — c'est ce qui permet
  // à Python de resynchroniser sim_clock_start_min (toutes les ~20 min simulées, ou au
  // play/pause) sans provoquer de saut visuel ni de perte de zoom.
  useEffect(() => {
    baselineRef.current = { simClockStartMin, autoRun, startTime: performance.now() };
  }, [simClockStartMin, autoRun]);

  useEffect(() => {
    if (mapRef.current) {
      mapRef.current.remove();
      mapRef.current = null;
    }
    if (!mapContainerRef.current) return undefined;

    const map = L.map(mapContainerRef.current).setView(depot, 11);
    mapRef.current = map;
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors",
      maxZoom: 19,
    }).addTo(map);

    L.marker(depot, {
      icon: L.divIcon({ className: "depot-icon", html: "🏠", iconSize: [24, 24] }),
    })
      .addTo(map)
      .bindPopup("<b>Dépôt Central — Oued Smar</b>");

    // Légende des priorités — repère visuel permanent pour distinguer les commandes
    // urgentes / haute importance des commandes normales sur la carte.
    const legend = L.control({ position: "bottomright" });
    legend.onAdd = () => {
      const div = L.DomUtil.create("div", "dvrp-legend");
      div.innerHTML = ["URGENTE", "HAUTE", "NORMALE"]
        .map(
          (p) =>
            '<div class="dvrp-legend-row"><span class="dvrp-legend-dot" style="background:' +
            PRIORITY_COLOR[p] + '"></span>' + PRIORITY_LABEL[p] + "</div>"
        )
        .join("");
      return div;
    };
    legend.addTo(map);

    const orderMarkers = {};
    orderPriorityRef.current = Object.fromEntries(orders.map((o) => [o.id, o.priority]));
    orders.forEach((o) => {
      const color = PRIORITY_COLOR[o.priority] || PRIORITY_COLOR.NORMALE;
      const isHighPriority = HIGH_PRIORITIES.has(o.priority);
      const radius = PRIORITY_RADIUS[o.priority] || PRIORITY_RADIUS.NORMALE;

      // Halo pulsant derrière le marqueur, uniquement pour URGENTE/HAUTE — permet de
      // repérer ces commandes en un coup d'œil sur la carte, même à faible zoom.
      let haloMarker = null;
      if (isHighPriority) {
        haloMarker = L.circleMarker([o.lat, o.lon], {
          radius: radius + 6, color, fillColor: color, fillOpacity: 0.25,
          weight: 0, className: "dvrp-priority-halo",
        });
      }

      const marker = L.circleMarker([o.lat, o.lon], {
        radius, color, fillColor: color, fillOpacity: 0.9,
        weight: isHighPriority ? 3 : 2,
      }).bindPopup(
        "<b>" + o.client + "</b><br>" + (PRIORITY_LABEL[o.priority] || o.priority) + "<br>" +
          "Charge: " + o.demand_kg + " kg<br>" +
          "Temp. max: " + o.temp_max + "°C<br>Fenêtre: " + o.time_window
      ).bindTooltip(o.id + (isHighPriority ? " ⚠️" : ""));
      orderMarkers[o.id] = { marker, haloMarker, shown: false };
    });
    orderMarkersRef.current = orderMarkers;
    // Amorce avec les commandes déjà visibles au moment du montage (celles dont le
    // release_time est déjà passé) : sinon la toute première frame les signalerait
    // TOUTES comme "nouvellement arrivées", déclenchant un rerun Python inutile.
    reportedVisibleIdsRef.current = new Set(
      orders.filter((o) => o.release_time <= simClockStartMin).map((o) => o.id)
    );

    const truckState = {};
    let usedCount = 0;
    trucks.forEach((t) => {
      const shape = t.shape || [];
      const cumKm = [0];
      for (let i = 1; i < shape.length; i++) {
        cumKm.push(
          cumKm[cumKm.length - 1] +
            haversineKm(shape[i - 1][0], shape[i - 1][1], shape[i][0], shape[i][1])
        );
      }
      if (t.used && shape.length && t.trip_shapes) {
        t.trip_shapes.forEach((path) => {
          L.polyline(path, { color: t.color, weight: 4, opacity: 0.85 }).addTo(map);
        });
      }
      const startPos = t.used && shape.length ? interpolate(shape, cumKm, 0) : depot;
      const marker = L.marker(startPos, {
        icon: L.divIcon({ className: "truck-icon", html: "🚚", iconSize: [24, 24] }),
      })
        .addTo(map)
        .bindTooltip(t.label);
      truckState[t.label] = {
        marker, shape, cumKm, used: t.used,
        stops: t.stops || [], totalKm: t.total_km || 0, speedKmh: t.speed_kmh || 0,
      };
      if (t.used) usedCount += 1;
    });
    truckStateRef.current = truckState;

    let frameId;
    let lastUiUpdate = 0;

    function animate(now) {
      const { simClockStartMin: baseMin, autoRun: running } = baselineRef.current;
      const elapsedSec = (now - baselineRef.current.startTime) / 1000;
      const simClockMin = running ? baseMin + elapsedSec * simMinutesPerRealSecond : baseMin;

      orders.forEach((o) => {
        const entry = orderMarkersRef.current[o.id];
        if (!entry) return;
        const shouldShow = o.release_time <= simClockMin;
        if (shouldShow && !entry.shown) {
          if (entry.haloMarker) entry.haloMarker.addTo(map);
          entry.marker.addTo(map);
          entry.shown = true;
        } else if (!shouldShow && entry.shown) {
          if (entry.haloMarker) entry.haloMarker.remove();
          entry.marker.remove();
          entry.shown = false;
        }
      });

      // Amorcé avec l'historique (alreadyDeliveredIds) : une commande déjà livrée
      // lors d'un PRÉCÉDENT plan reste comptée, même si elle n'apparaît plus du
      // tout dans les tournées de ce nouveau plan (normal : elle est exclue du
      // routage une fois livrée — voir dvrp_engine / app.py).
      const deliveredSet = new Set(alreadyDeliveredIds);
      // Distance parcourue DANS CE SEGMENT (depuis le dernier recalcul OR-Tools) —
      // le cumul réel affiché/renvoyé ajoute `alreadyTraveledKm` plus bas.
      let segmentDistanceKm = 0;
      let allUsedFinished = usedCount > 0;
      Object.values(truckStateRef.current).forEach((s) => {
        if (!s.used) return;
        const traveledKm = Math.min(s.totalKm, (s.speedKmh * (simClockMin - baseMin)) / 60);
        segmentDistanceKm += traveledKm;
        if (traveledKm < s.totalKm) allUsedFinished = false;
        const pos = interpolate(s.shape, s.cumKm, traveledKm);
        if (pos) s.marker.setLatLng(pos);
        s.stops.forEach((stop) => {
          if (traveledKm >= stop.cum_km) deliveredSet.add(stop.order_id);
        });
      });
      // Suivi urgent/haute importance PAR TOURNÉE : pour chaque camion, on compte les
      // arrêts prioritaires (URGENTE/HAUTE) pas encore livrés dans SA tournée — c'est
      // ce qui permet d'afficher "V2 : 2 commandes urgentes en cours" dans le panneau
      // dédié ci-dessous, sans dépendre d'un recalcul Python.
      const truckPriorityCounts = {};
      Object.entries(truckStateRef.current).forEach(([label, s]) => {
        if (!s.used) return;
        const traveledKm = Math.min(s.totalKm, (s.speedKmh * (simClockMin - baseMin)) / 60);
        const pending = s.stops.filter(
          (stop) =>
            traveledKm < stop.cum_km &&
            HIGH_PRIORITIES.has(orderPriorityRef.current[stop.order_id])
        ).length;
        if (pending > 0) truckPriorityCounts[label] = pending;
      });

      // Suivi urgent/haute importance GLOBAL : commandes déjà apparues, prioritaires,
      // et pas encore livrées — toutes tournées confondues (pour la carte / le KPI).
      const highPriorityPending = orders.filter(
        (o) =>
          o.release_time <= simClockMin &&
          HIGH_PRIORITIES.has(o.priority) &&
          !deliveredSet.has(o.id)
      );

      // CORRECTIF (distance parcourue erronée) : `segmentDistanceKm` seul ne
      // mesurait que la distance parcourue DEPUIS LE DERNIER RECALCUL OR-TOOLS,
      // pas depuis le début de la simulation — puisqu'un recalcul (à chaque
      // livraison ou nouvelle commande) remonte tout le composant et fait
      // repartir chaque camion virtuellement de son point de départ de tournée.
      // Le vrai cumulatif = ce qui était déjà acquis avant ce recalcul + le
      // segment courant.
      const distanceParcourue = alreadyTraveledKm + segmentDistanceKm;

      if (now - lastUiUpdate > 250) {
        lastUiUpdate = now;
        const visibleCount = orders.filter((o) => o.release_time <= simClockMin).length;
        const deliveredIds = Array.from(deliveredSet);

        // CORRECTIF (voir reportedVisibleIdsRef ci-dessus) : détecte la ou les commandes
        // qui viennent de franchir leur release_time depuis le dernier report, et en
        // signale UNE à Python via new_order_arrived. Une seule suffit : ça déclenche
        // la resynchronisation de l'horloge Python (sim_clock_min) sur l'horloge réelle
        // du composant, ce qui fait automatiquement entrer TOUTES les commandes arrivées
        // entre-temps dans `active_orders` côté Python (filtre release_time <= sim_time),
        // pas seulement celle explicitement nommée.
        let newlyArrivedId = null;
        for (const o of orders) {
          if (o.release_time <= simClockMin && !reportedVisibleIdsRef.current.has(o.id)) {
            newlyArrivedId = o.id;
            break;
          }
        }

        setKpi({
          simClockMin, visibleCount, totalOrders: orders.length,
          deliveredIds, distanceParcourue, allFinished: allUsedFinished,
          highPriorityPending, truckPriorityCounts,
        });
        // `visibleCount` dans la clé : toute nouvelle commande qui "arrive" (release_time
        // atteint) déclenche aussitôt un retour vers Python, qui relance alors une
        // réoptimisation OR-Tools pour l'intégrer au plan — c'est ce qui rend la
        // planification (et donc "Séquence de l'itinéraire") réellement dynamique.
        const reportKey = JSON.stringify([deliveredIds.sort(), allUsedFinished, visibleCount, Math.floor(simClockMin / 20)]);
        if (reportKey !== lastReportedRef.current) {
          lastReportedRef.current = reportKey;
          // On renvoie aussi l'horloge courante à Python à cette occasion (pas d'appel
          // supplémentaire créé exprès pour ça) : ça évite qu'un événement déclenché en
          // cours de route (ex. panne véhicule) ne reparte d'une horloge Python périmée
          // et ne fasse "reculer" visuellement les camions au rechargement du composant.
          // Le checkpoint toutes les ~20 min simulées borne l'écart même sans livraison.
          Streamlit.setComponentValue({
            delivered_ids: deliveredIds, all_finished: allUsedFinished, sim_clock_min: simClockMin,
            // Nouveau champ : permet à Python de conserver ce cumul comme point de
            // départ ("checkpoint") du prochain recalcul OR-Tools, au lieu de
            // perdre la distance déjà parcourue à chaque réoptimisation.
            distance_parcourue_km: distanceParcourue,
            new_order_arrived: newlyArrivedId,
          });
          if (newlyArrivedId) {
            // Marqué comme signalé tout de suite : le rerun Python qui va suivre va de
            // toute façon remonter le composant avec un nouveau structuralKey (nouvel
            // active_orders → nouvelles tournées), donc ce ref n'a de toute façon plus
            // besoin de survivre au-delà de cette frame — mais on évite ainsi de le
            // resignaler en boucle si le rerun tarde un peu.
            reportedVisibleIdsRef.current.add(newlyArrivedId);
          }
        }
      }
      frameId = requestAnimationFrame(animate);
    }
    frameId = requestAnimationFrame(animate);

    return () => {
      cancelAnimationFrame(frameId);
      if (mapRef.current) {
        mapRef.current.remove();
        mapRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [structuralKey]);

  useEffect(() => {
    if (wrapperRef.current) {
      Streamlit.setFrameHeight(wrapperRef.current.scrollHeight + 20);
    }
  });

  const deliveredSet = new Set(kpi.deliveredIds);

  // Le tableau n'affiche QUE les commandes déjà apparues (release_time atteint) ou
  // annulées — les commandes "à venir" (pas encore révélées) sont volontairement
  // masquées pour ne pas encombrer l'affichage avec des informations pas encore
  // pertinentes. Chaque commande visible indique clairement si elle a été livrée.
  const tableRows = orders
    .filter((o) => o.release_time <= kpi.simClockMin)
    .map((o) => {
      let status;
      if (deliveredSet.has(o.id)) status = "✅ Livrée";
      else if (o.is_new) status = "🆕 Nouvelle — en livraison";
      else status = "🚚 En livraison";
      return { id: o.id, client: o.client, demand_kg: o.demand_kg, priority: o.priority, status };
    })
    .concat(
      cancelledOrders.map((o) => ({
        id: o.id, client: o.client, demand_kg: o.demand_kg, priority: o.priority, status: "❌ Retirée",
      }))
    );

  return (
    <div ref={wrapperRef} style={{ fontFamily: "-apple-system, Segoe UI, Roboto, sans-serif" }}>
      <style>{
        ".truck-icon { font-size: 22px; line-height: 22px; text-align: center;" +
        "  filter: drop-shadow(0 0 2px rgba(0,0,0,.6)); }" +
        ".depot-icon { font-size: 22px; line-height: 22px; text-align: center; }" +
        ".dvrp-map-box { box-shadow: 0 1px 4px rgba(0,0,0,.12); }" +
        ".dvrp-kpis { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; }" +
        ".dvrp-kpi-card { flex: 1; min-width: 140px; background: #f7f8fa; border: 1px solid #edeef2;" +
        "  border-radius: 10px; padding: 10px 14px; transition: box-shadow .15s; }" +
        ".dvrp-kpi-card:hover { box-shadow: 0 2px 6px rgba(0,0,0,.08); }" +
        ".dvrp-kpi-label { font-size: 12px; color: #666; display: flex; align-items: center; gap: 5px; }" +
        ".dvrp-kpi-value { font-size: 22px; font-weight: 650; margin-top: 2px; color: #1a1a1a; }" +
        ".dvrp-kpi-sub { font-size: 11px; color: #999; margin-top: 1px; }" +
        ".dvrp-live-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; }" +
        ".dvrp-live-dot.on { background: #2ecc71; box-shadow: 0 0 0 3px rgba(46,204,113,.25); }" +
        ".dvrp-live-dot.off { background: #bbb; }" +
        ".dvrp-final { margin-top: 12px; border-radius: 10px; padding: 12px 16px; line-height: 1.6; }" +
        ".dvrp-final.done { background: #e9f9ee; border: 1px solid #b7ebc6; }" +
        ".dvrp-final.pending { background: #fff8e1; border: 1px solid #ffe7a0; }" +
        ".dvrp-table-wrap { margin-top: 12px; max-height: 260px; overflow-y: auto; border: 1px solid #edeef2;" +
        "  border-radius: 10px; }" +
        ".dvrp-table { width: 100%; border-collapse: collapse; font-size: 13px; }" +
        ".dvrp-table th, .dvrp-table td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #f3f3f3; }" +
        ".dvrp-table th { position: sticky; top: 0; background: #fafbfc; font-weight: 600; color: #555; }" +
        ".dvrp-table tr:hover td { background: #fafbfc; }" +
        ".dvrp-empty { padding: 14px; text-align: center; color: #999; font-size: 13px; }" +
        ".dvrp-kpi-card.dvrp-kpi-alert { background: #fff3ef; border-color: #ffd0bf; }" +
        ".dvrp-kpi-card.dvrp-kpi-alert .dvrp-kpi-value { color: #d84315; }" +
        ".dvrp-priority-panel { margin-top: 10px; background: #fff8f5; border: 1px solid #ffe0d0;" +
        "  border-radius: 10px; padding: 10px 14px; }" +
        ".dvrp-priority-panel-title { font-size: 12px; font-weight: 650; color: #b34700; margin-bottom: 6px; }" +
        ".dvrp-priority-chips { display: flex; flex-wrap: wrap; gap: 8px; }" +
        ".dvrp-priority-chip { font-size: 12.5px; background: #fff; border: 1px solid #ffd0b8;" +
        "  border-radius: 20px; padding: 4px 10px; color: #333; }" +
        ".dvrp-row-urgente td { background: #fdecea; }" +
        ".dvrp-row-haute td { background: #fff4e5; }" +
        // CORRECTIF (teinte de priorité invisible au survol) : ".dvrp-table tr:hover td"
        // (2 classes/pseudo-classes + 2 éléments) est PLUS spécifique que
        // ".dvrp-row-urgente td" (1 classe + 1 élément) — donc le survol de la souris
        // écrasait systématiquement la couleur rouge/orange par le gris générique,
        // rendant une commande prioritaire indiscernable pile au moment où on la lit.
        // On préfixe par ".dvrp-table" pour dépasser la spécificité du survol générique.
        ".dvrp-table .dvrp-row-urgente:hover td { background: #f7d9d6; }" +
        ".dvrp-table .dvrp-row-haute:hover td { background: #ffe6c2; }" +
        ".dvrp-legend { background: rgba(255,255,255,.92); padding: 8px 10px; border-radius: 8px;" +
        "  box-shadow: 0 1px 4px rgba(0,0,0,.2); font-size: 12px; line-height: 1.6; }" +
        ".dvrp-legend-row { display: flex; align-items: center; gap: 6px; }" +
        ".dvrp-legend-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }" +
        ".dvrp-priority-halo { animation: dvrp-pulse 1.4s ease-in-out infinite; }" +
        "@keyframes dvrp-pulse { 0% { opacity: .55; } 50% { opacity: .1; } 100% { opacity: .55; } }"
      }</style>

      <div ref={mapContainerRef} className="dvrp-map-box"
        style={{ width: "100%", height: height + "px", borderRadius: 10 }} />

      <div className="dvrp-kpis">
        <div className="dvrp-kpi-card">
          <div className="dvrp-kpi-label">
            <span className={"dvrp-live-dot " + (autoRun ? "on" : "off")} />
            Horloge de simulation
          </div>
          <div className="dvrp-kpi-value">{fmtClock(kpi.simClockMin)}</div>
          <div className="dvrp-kpi-sub">{autoRun ? "En direct" : "En pause"}</div>
        </div>
        <div className="dvrp-kpi-card">
          <div className="dvrp-kpi-label">📋 Commandes arrivées</div>
          <div className="dvrp-kpi-value">{kpi.visibleCount}</div>
        </div>
        <div className="dvrp-kpi-card">
          <div className="dvrp-kpi-label">✅ Commandes livrées</div>
          <div className="dvrp-kpi-value">{kpi.deliveredIds.length}</div>
        </div>
        <div className="dvrp-kpi-card">
          <div className="dvrp-kpi-label">📏 Distance parcourue</div>
          <div className="dvrp-kpi-value">{kpi.distanceParcourue.toFixed(1)} km</div>
        </div>
        <div className="dvrp-kpi-card">
          <div className="dvrp-kpi-label">🚛 Camions en tournée</div>
          <div className="dvrp-kpi-value">{numVehiclesUsed} / {numVehiclesTotal}</div>
        </div>
        <div className={"dvrp-kpi-card" + (kpi.highPriorityPending.length > 0 ? " dvrp-kpi-alert" : "")}>
          <div className="dvrp-kpi-label">⚠️ Prioritaires en attente</div>
          <div className="dvrp-kpi-value">{kpi.highPriorityPending.length}</div>
          <div className="dvrp-kpi-sub">Urgentes + haute importance</div>
        </div>
      </div>

      {Object.keys(kpi.truckPriorityCounts).length > 0 && (
        <div className="dvrp-priority-panel">
          <div className="dvrp-priority-panel-title">📦 Suivi par tournée — commandes prioritaires en cours</div>
          <div className="dvrp-priority-chips">
            {Object.entries(kpi.truckPriorityCounts).map(([label, count]) => (
              <span key={label} className="dvrp-priority-chip">
                🚚 {label} · <b>{count}</b> {count > 1 ? "commandes" : "commande"} prioritaire{count > 1 ? "s" : ""}
              </span>
            ))}
          </div>
        </div>
      )}

      {kpi.allFinished ? (
        <div className="dvrp-final done">
          <b>🎉 Tournée terminée — résultats finaux</b><br />
          📏 Distance planifiée : <b>{plannedDistanceKm.toFixed(1)} km</b> (vs {baselineDistanceKm.toFixed(1)} km
          sans optimisation, soit <b>-{gainPct.toFixed(0)} %</b>)<br />
          ✅ Commandes livrées : <b>{kpi.deliveredIds.length} / {kpi.totalOrders}</b>
          {" "}· ⏱️ Temps simulé écoulé : <b>{fmtClock(kpi.simClockMin)}</b>
        </div>
      ) : (
        <div className="dvrp-final pending">
          ⏳ Simulation en cours — <b>{kpi.deliveredIds.length}</b> commande(s) livrée(s) sur{" "}
          <b>{kpi.totalOrders}</b>. Les résultats finaux (distance, gain d'optimisation) s'afficheront
          ici une fois la tournée terminée.
        </div>
      )}

      <div className="dvrp-table-wrap">
        {tableRows.length === 0 ? (
          <div className="dvrp-empty">Aucune commande visible pour le moment.</div>
        ) : (
          <table className="dvrp-table">
            <thead>
              <tr><th>ID</th><th>Client</th><th>Kg</th><th>Priorité</th><th>État</th></tr>
            </thead>
            <tbody>
              {tableRows.map((r) => (
                <tr
                  key={r.id}
                  className={
                    r.priority === "URGENTE" ? "dvrp-row-urgente"
                      : r.priority === "HAUTE" ? "dvrp-row-haute"
                      : ""
                  }
                >
                  <td>{r.id}</td><td>{r.client}</td><td>{r.demand_kg}</td>
                  <td>{PRIORITY_LABEL[r.priority] || r.priority}</td><td>{r.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

const ConnectedDvrpMap = withStreamlitConnection(DvrpMap);
const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(<ConnectedDvrpMap />);
