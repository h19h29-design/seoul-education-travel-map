const MAP_SCRIPT_ID = "kakao-map-sdk";
let sdkPromise;

function coordinate(point) {
  return new window.kakao.maps.LatLng(point.latitude, point.longitude);
}

function messageForMapError() {
  return "지도를 불러오지 못했습니다. 등록 도메인과 지도 JavaScript 키를 확인해 주세요.";
}

function flattenCoordinates(coordinates, output = []) {
  if (typeof coordinates?.[0] === "number") {
    output.push({ latitude: coordinates[1], longitude: coordinates[0] });
    return output;
  }
  coordinates?.forEach((item) => flattenCoordinates(item, output));
  return output;
}

function loadScript(key) {
  if (sdkPromise) return sdkPromise;
  sdkPromise = new Promise((resolve, reject) => {
    const existing = document.getElementById(MAP_SCRIPT_ID);
    if (existing) {
      existing.addEventListener("load", resolve, { once: true });
      existing.addEventListener("error", reject, { once: true });
      return;
    }
    const script = document.createElement("script");
    script.id = MAP_SCRIPT_ID;
    script.async = true;
    script.src = `https://dapi.kakao.com/v2/maps/sdk.js?autoload=false&appkey=${encodeURIComponent(key)}`;
    script.addEventListener("load", resolve, { once: true });
    script.addEventListener("error", reject, { once: true });
    document.head.append(script);
  });
  return sdkPromise;
}

export class KakaoMapController {
  constructor(element, statusElement, onMapClick = null) {
    this.element = element;
    this.statusElement = statusElement;
    this.onMapClick = onMapClick;
    this.map = null;
    this.originCandidate = null;
    this.originCandidateMarker = null;
    this.destinationCandidate = null;
    this.destinationCandidateMarker = null;
    this.activeRouteIds = new Map();
    this.routeLines = new Map();
    this.routeOptions = new Map();
    this.overlays = [];
    this.boundaries = [];
  }

  setStatus(message) {
    this.statusElement.textContent = message;
  }

  async initialize(key) {
    if (!key) {
      this.setStatus("등록된 지도 키가 없어 지도를 표시할 수 없습니다.");
      return false;
    }
    try {
      if (!window.kakao?.maps) await loadScript(key);
      await new Promise((resolve) => window.kakao.maps.load(resolve));
      const maps = window.kakao.maps;
      this.map = new maps.Map(this.element, {
        center: new maps.LatLng(37.5665, 126.978),
        level: 8,
        mapTypeId: maps.MapTypeId?.ROADMAP,
      });
      this.element.classList.add("is-ready");
      this.setStatus("지도에서 위치를 선택해 출장지 주소를 확인할 수 있습니다.");
      maps.event?.addListener?.(this.map, "click", async (event) => {
        const latitude = event.latLng.getLat();
        const longitude = event.latLng.getLng();
        await this.onMapClick?.({ latitude, longitude });
      });
      this.renderOriginCandidate();
      this.renderDestinationCandidate();
      return true;
    } catch {
      this.setStatus(messageForMapError());
      return false;
    }
  }

  clearRouteOverlays() {
    this.overlays.forEach((overlay) => overlay.setMap?.(null));
    this.overlays = [];
    this.routeLines.clear();
    this.routeOptions.clear();
    this.activeRouteIds.clear();
    this.element.dataset.activeRoutes = "";
  }

  setClickHandler(handler) {
    this.onMapClick = handler;
  }

  replaceCandidateMarker(property, point, title) {
    this[property]?.setMap?.(null);
    this[property] = null;
    if (!this.map || !window.kakao?.maps || !point) return;
    const marker = new window.kakao.maps.Marker({
      map: this.map,
      position: coordinate(point),
      title,
    });
    this[property] = marker;
    this.map.panTo?.(coordinate(point));
  }

  renderOriginCandidate() {
    if (!this.originCandidate) return;
    this.replaceCandidateMarker(
      "originCandidateMarker",
      this.originCandidate.coordinate,
      this.originCandidate.displayName,
    );
  }

  showOriginCandidate(placeOrSite) {
    this.originCandidate = placeOrSite;
    this.renderOriginCandidate();
  }

  clearOriginCandidate() {
    this.originCandidate = null;
    this.originCandidateMarker?.setMap?.(null);
    this.originCandidateMarker = null;
  }

  renderDestinationCandidate() {
    if (!this.destinationCandidate) return;
    this.replaceCandidateMarker(
      "destinationCandidateMarker",
      this.destinationCandidate,
      this.destinationCandidate.name,
    );
  }

  showDestinationCandidate(place) {
    this.destinationCandidate = place;
    this.renderDestinationCandidate();
  }

  clearDestinationCandidate() {
    this.destinationCandidate = null;
    this.destinationCandidateMarker?.setMap?.(null);
    this.destinationCandidateMarker = null;
  }

  routeKey(direction, routeId) {
    return `${direction}:${routeId}`;
  }

  updateActiveRouteDataset() {
    this.element.dataset.activeRoutes = [...this.activeRouteIds.entries()]
      .map(([direction, routeId]) => this.routeKey(direction, routeId))
      .join(" ");
  }

  createRouteLine(route, direction) {
    const maps = window.kakao.maps;
    const line = new maps.Polyline({
      map: this.map,
      path: route.geometry.map(coordinate),
      strokeWeight: 8,
      strokeColor: direction === "OUTBOUND" ? "#2d6cdf" : "#4b9f88",
      strokeOpacity: 1,
      strokeStyle: route.mode === "WALK" ? "shortdash" : "solid",
    });
    this.overlays.push(line);
    return line;
  }

  showRoutes({ origin, destination, routeLegs, selectedRouteIdsByDirection, classificationPath }) {
    if (!this.map || !window.kakao?.maps) return;
    this.clearRouteOverlays();
    const maps = window.kakao.maps;
    const bounds = new maps.LatLngBounds();
    const addPoint = (point) => bounds.extend(coordinate(point));
    [origin.coordinate, destination].forEach(addPoint);
    routeLegs.forEach((leg) => {
      leg.routes.forEach((route) => {
        route.geometry.forEach(addPoint);
        this.routeOptions.set(this.routeKey(leg.direction, route.id), { direction: leg.direction, route });
      });
    });
    if (classificationPath?.geometry?.length) {
      const classificationLine = new maps.Polyline({
        map: this.map,
        path: classificationPath.geometry.map(coordinate),
        strokeWeight: 2,
        strokeColor: "#1f5fbf",
        strokeOpacity: 0.75,
        strokeStyle: "shortdash",
      });
      this.overlays.push(classificationLine);
    }
    this.map.setBounds?.(bounds);
    Object.entries(selectedRouteIdsByDirection).forEach(([direction, routeId]) => {
      this.setActiveRoute(direction, routeId);
    });
  }

  setActiveRoutes(routeIds) {
    const next = new Map(routeIds.map((key) => {
      const separator = key.indexOf(":");
      return [key.slice(0, separator), key.slice(separator + 1)];
    }));
    [...this.activeRouteIds.keys()]
      .filter((direction) => !next.has(direction))
      .forEach((direction) => {
        const routeId = this.activeRouteIds.get(direction);
        const key = this.routeKey(direction, routeId);
        this.routeLines.get(key)?.setMap?.(null);
        this.routeLines.delete(key);
        this.activeRouteIds.delete(direction);
      });
    next.forEach((routeId, direction) => this.setActiveRoute(direction, routeId));
  }

  setActiveRoute(direction, routeId) {
    if (!this.map || !window.kakao?.maps) return;
    const key = this.routeKey(direction, routeId);
    const option = this.routeOptions.get(key);
    if (!option || this.activeRouteIds.get(direction) === routeId) return;
    const previousRouteId = this.activeRouteIds.get(direction);
    if (previousRouteId) {
      const previousKey = this.routeKey(direction, previousRouteId);
      this.routeLines.get(previousKey)?.setMap?.(null);
      this.routeLines.delete(previousKey);
    }
    this.activeRouteIds.set(direction, routeId);
    this.routeLines.set(key, this.createRouteLine(option.route, direction));
    this.updateActiveRouteDataset();
  }

  async setBoundary(name, visible, fetchGeojson) {
    if (!this.map || !window.kakao?.maps) return;
    this.boundaries
      .filter((item) => item.datasetName === name)
      .forEach((item) => item.setMap?.(null));
    this.boundaries = this.boundaries.filter((item) => item.datasetName !== name);
    if (!visible) return;
    try {
      const geojson = await fetchGeojson(name);
      const maps = window.kakao.maps;
      const points = flattenCoordinates(geojson.features?.flatMap((feature) => feature.geometry?.coordinates));
      if (!points.length) return;
      const polygon = new maps.Polygon({
        map: this.map,
        path: points.map(coordinate),
        strokeWeight: 2,
        strokeColor: name === "seoul" ? "#2d6cdf" : "#3ea17a",
        strokeOpacity: 0.8,
        strokeStyle: "shortdash",
        fillOpacity: 0,
      });
      polygon.datasetName = name;
      this.boundaries.push(polygon);
    } catch {
      this.setStatus("보조 경계 데이터를 불러오지 못했습니다.");
    }
  }
}
