/* Hub-owned glue. Personal labels and notes are never inserted as HTML. */
"use strict";
let hub, map, sequence = 0, features = [], pending = new Map();
const empty = {type: "FeatureCollection", features: []};
new QWebChannel(qt.webChannelTransport, channel => {
    hub = channel.objects.hub;
    hub.resourceReady.connect((id, encoded, error) => {
        const entry = pending.get(id);
        if (!entry) return;
        pending.delete(id);
        if (error) { entry.reject(new Error(error)); return; }
        try {
            const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
            const data = entry.type === "json" ? JSON.parse(new TextDecoder().decode(bytes)) : bytes.buffer;
            entry.resolve({data});
        } catch (_) { entry.reject(new Error("Invalid map resource")); }
    });
    maplibregl.addProtocol("hubmap", (params, controller) => new Promise((resolve, reject) => {
        const id = ++sequence;
        pending.set(id, {resolve, reject, type: params.type});
        controller.signal.addEventListener("abort", () => {
            pending.delete(id);
            hub.cancelResource(id);
            reject(new Error("Map request cancelled"));
        }, {once: true});
        hub.requestResource(id, params.url.replace(/^hubmap:\/\//, "https://"));
    }));
    hub.loaded();
});
window.startMap = position => {
    if (map) return;
    try {
    map = new maplibregl.Map({
        container: "map", style: "hubmap://tiles.openfreemap.org/styles/liberty",
        center: [position.longitude, position.latitude], zoom: position.zoom,
        maxZoom: 19, attributionControl: false, collectResourceTiming: false,
        renderWorldCopies: true, refreshExpiredTiles: false,
        transformRequest: url => ({url: url.replace(/^https:\/\//, "hubmap://")})
    });
    } catch (_) {
        document.getElementById("hint").textContent = "Map graphics could not initialize. Saved places remain available below.";
        hub.mapStatus(false);
        return;
    }
    map.addControl(new maplibregl.NavigationControl({showCompass: false}), "top-right");
    map.on("error", () => hub.mapStatus(false));
    map.on("load", () => {
        map.addSource("places", {type: "geojson", data: empty, cluster: true, clusterRadius: 40});
        map.addLayer({id: "clusters", type: "circle", source: "places", filter: ["has", "point_count"],
            paint: {"circle-color": "#245f91", "circle-radius": 21, "circle-stroke-width": 2, "circle-stroke-color": "#fff"}});
        map.addLayer({id: "counts", type: "symbol", source: "places", filter: ["has", "point_count"],
            layout: {"text-field": ["get", "point_count_abbreviated"], "text-size": 12,
                "text-font": ["Noto Sans Regular"]}, paint: {"text-color": "#fff"}});
        map.addLayer({id: "pins", type: "circle", source: "places", filter: ["!", ["has", "point_count"]],
            paint: {"circle-color": ["case", ["get", "visited"], "#20734c", "#bc6b21"],
                "circle-radius": ["case", ["get", "favorite"], 10, 7],
                "circle-stroke-color": "#fff", "circle-stroke-width": 2}});
        window.setPlaces(features);
        hub.mapStatus(true);
    });
    map.on("click", async event => {
        const hits = map.getLayer("pins") ? map.queryRenderedFeatures(event.point, {layers: ["pins", "clusters"]}) : [];
        if (hits.length && hits[0].properties.cluster) {
            const zoom = await map.getSource("places").getClusterExpansionZoom(hits[0].properties.cluster_id);
            map.easeTo({center: hits[0].geometry.coordinates, zoom});
        } else if (hits.length) {
            hub.select(hits[0].properties.id);
        } else {
            hub.pick(event.lngLat.lat, ((event.lngLat.lng + 180) % 360 + 360) % 360 - 180);
        }
    });
    map.on("moveend", () => {
        const center = map.getCenter();
        hub.moved(JSON.stringify({latitude: center.lat,
            longitude: ((center.lng + 180) % 360 + 360) % 360 - 180, zoom: map.getZoom()}));
    });
};
window.setPlaces = data => {
    features = data;
    if (map && map.getSource("places")) map.getSource("places").setData({type: "FeatureCollection", features});
};
window.goTo = position => {
    if (map) map.jumpTo({center: [position.longitude, position.latitude], zoom: position.zoom});
};
