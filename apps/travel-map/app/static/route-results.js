const modeName = { TRANSIT: "대중교통", CAR: "자동차", WALK: "도보" };
const directionCopy = {
  OUTBOUND: { description: "가는 길", title: "가는 길" },
  RETURN: { description: "돌아오는 길", title: "돌아오는 길" },
};
const bestName = {
  fastestRouteId: "최단시간",
  shortestRouteId: "최단거리",
  cheapestRouteId: "최저비용",
};

function formatMoney(value) {
  return value == null ? "비용 정보 없음" : `${new Intl.NumberFormat("ko-KR").format(value)}원`;
}

function formatDistance(value) {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}km` : `${value}m`;
}

function formatDuration(seconds) {
  const minutes = Math.round(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)}시간 ${minutes % 60}분` : `${minutes}분`;
}

function routeKey(direction, routeId) {
  return `${direction}:${routeId}`;
}

function warningText(warning) {
  return {
    PARKING_COST_ESTIMATED: "주차비는 예상값입니다",
    DISTANCE_EVIDENCE_UNAVAILABLE: "분류 경로 정보 확인 필요",
    PARTIAL_MOBILITY_COST: "이동비 일부 정보 확인 필요",
  }[warning] || "경로 정보 일부를 확인해 주세요";
}

function allowanceWarningText(warnings = []) {
  return warnings.map((warning) => (
    warning === "NON_LOCAL_ALLOWANCE_OUT_OF_SCOPE"
      ? "기관의 최종 관외 판단과 지급 기준을 확인하세요."
      : warningText(warning)
  )).join(" · ");
}

export function createRouteResults({ elements, map }) {
  let preview = null;
  let sort = "time";
  let selected = {};

  function sorted(routes) {
    const keys = {
      time: (route) => [route.durationSeconds, route.distanceMeters],
      distance: (route) => [route.distanceMeters, route.durationSeconds],
      cost: (route) => [route.mobilityCostKrw == null ? Infinity : route.mobilityCostKrw, route.durationSeconds],
    };
    return [...routes].sort((first, second) => {
      const [firstPrimary, firstSecondary] = keys[sort](first);
      const [secondPrimary, secondSecondary] = keys[sort](second);
      return firstPrimary - secondPrimary || firstSecondary - secondSecondary;
    });
  }

  function bestLabels(route, leg) {
    return Object.entries(bestName)
      .filter(([key]) => leg.best?.[key] === route.id)
      .map(([, label]) => label);
  }

  function routeCard(direction, leg, route) {
    const card = document.createElement("button");
    const key = routeKey(direction, route.id);
    const routeMode = { TRANSIT: "transit", CAR: "car", WALK: "walk" }[route.mode] || "unknown";
    card.type = "button";
    card.className = `route-card mode-${routeMode}`;
    card.dataset.routeId = key;
    card.setAttribute("aria-current", String(selected[direction] === route.id));
    const top = document.createElement("span");
    top.className = "route-top";
    const title = document.createElement("strong");
    title.textContent = `${modeName[route.mode] || "이동 경로"} · ${formatDuration(route.durationSeconds)}`;
    const badges = document.createElement("span");
    badges.className = "route-badges";
    bestLabels(route, leg).forEach((label) => {
      const badge = document.createElement("span");
      badge.className = "route-badge";
      badge.textContent = label;
      badges.append(badge);
    });
    top.append(title, badges);

    const metrics = document.createElement("span");
    metrics.className = "route-metrics";
    [
      ["소요시간", formatDuration(route.durationSeconds)],
      ["거리", formatDistance(route.distanceMeters)],
      ["예상 이동비", formatMoney(route.mobilityCostKrw)],
    ].forEach(([label, value]) => {
      const metric = document.createElement("span");
      const metricLabel = document.createElement("span");
      const metricValue = document.createElement("b");
      metricLabel.textContent = label;
      metricValue.textContent = value;
      metric.append(metricLabel, metricValue);
      metrics.append(metric);
    });
    card.append(top, metrics);
    if (route.warnings?.length) {
      const warning = document.createElement("span");
      warning.className = "route-warning";
      warning.textContent = `⚠ ${route.warnings.map(warningText).join(" · ")}`;
      card.append(warning);
    }
    const source = document.createElement("span");
    source.className = "route-source";
    source.textContent = `${route.source} 기준`;
    card.append(source);
    card.addEventListener("click", () => {
      selected[direction] = route.id;
      map.setActiveRoute(direction, route.id);
      renderLegs();
    });
    return card;
  }

  function renderLegs() {
    const legs = preview?.routeLegs || [];
    elements.routeCount.textContent = `${legs.reduce((count, leg) => count + leg.routes.length, 0)}개`;
    elements.routeList.replaceChildren();
    legs.forEach((leg) => {
      const direction = leg.direction;
      const section = document.createElement("section");
      section.className = "route-leg";
      section.dataset.routeDirection = direction;
      const heading = document.createElement("h3");
      heading.textContent = directionCopy[direction]?.title || direction;
      const description = document.createElement("p");
      description.className = "route-leg-description";
      description.textContent = directionCopy[direction]?.description || direction;
      const cards = document.createElement("div");
      cards.className = "route-leg-cards";
      sorted(leg.routes).forEach((route) => cards.append(routeCard(direction, leg, route)));
      section.append(heading, description, cards);
      elements.routeList.append(section);
    });
  }

  function renderSummary() {
    const {
      allowance,
      classification,
      classificationDistanceBasis,
      classificationDistanceMeters,
      coverage,
      mobilityCost,
    } = preview;
    elements.coverageStatus.textContent = coverage.status === "SEOUL" ? "서울 경계 내" : coverage.status;
    elements.classificationResult.textContent = {
      LOCAL: "관내출장",
      NON_LOCAL_EXPECTED: "관외 예상",
    }[classification] || "판정 보류";
    elements.classificationDistance.textContent = classificationDistanceBasis === "ROUND_TRIP_EXACT"
      ? `왕복 확인 거리 ${formatDistance(classificationDistanceMeters)}`
      : classificationDistanceBasis === "ONE_WAY_LOWER_BOUND"
        ? `편도 확인 거리(하한) ${formatDistance(classificationDistanceMeters)}`
        : "거리 근거 없음 · 지급액 검토 필요";
    elements.classificationWarning.textContent = allowanceWarningText(allowance.warnings);
    elements.classificationWarning.hidden = !elements.classificationWarning.textContent;
    elements.mobilityCost.textContent = formatMoney(mobilityCost.amountKrw);
    elements.mobilityStatus.textContent = mobilityCost.status === "ESTIMATED"
      ? "예상값"
      : mobilityCost.status === "UNKNOWN"
        ? "확인 필요"
        : "경로 기준";
    elements.mobilityWarning.textContent = (mobilityCost.warnings || [])
      .map(warningText)
      .join(" · ");
    elements.mobilityWarning.hidden = !elements.mobilityWarning.textContent;
    const allowanceNeedsReview = allowance.status === "REVIEW_REQUIRED" || allowance.amountKrw == null;
    elements.allowanceAmount.textContent = allowanceNeedsReview ? "여비 판정 보류" : formatMoney(allowance.amountKrw);
    elements.allowanceStatus.textContent = allowanceNeedsReview ? "신분·규정을 확인하세요" : "지급 확정액 아님";
  }

  function render(nextPreview, destination) {
    preview = nextPreview;
    selected = Object.fromEntries(preview.routeLegs.flatMap((leg) => {
      const routeId = leg.best?.fastestRouteId || leg.routes[0]?.id;
      return routeId ? [[leg.direction, routeId]] : [];
    }));
    elements.results.hidden = false;
    renderSummary();
    renderLegs();
    map.showRoutes({
      origin: preview.origin,
      destination,
      routeLegs: preview.routeLegs,
      selectedRouteIdsByDirection: selected,
      classificationPath: preview.classificationPath,
    });
  }

  function clear() {
    preview = null;
    selected = {};
    elements.results.hidden = true;
    elements.routeList.replaceChildren();
    elements.routeCount.textContent = "";
    map.clearRouteOverlays();
  }

  function setSort(nextSort) {
    if (!new Set(["time", "distance", "cost"]).has(nextSort)) return;
    sort = nextSort;
    if (preview) renderLegs();
  }

  return {
    clear,
    render,
    selectedRouteIdsByDirection: () => ({ ...selected }),
    setSort,
  };
}
