function selected(picker) {
  return picker.selected?.() || null;
}

function destinationPayload(destination) {
  return {
    name: destination.name,
    address: destination.roadAddress || destination.lotAddress,
    latitude: destination.latitude,
    longitude: destination.longitude,
  };
}

export function createTripForm({ originPicker, destinationPicker, schedule, elements }) {
  function valid() {
    return Boolean(
      selected(originPicker)
      && selected(destinationPicker)
      && schedule.valid()
      && elements.form.checkValidity(),
    );
  }

  function payload() {
    if (!valid()) return null;
    const origin = selected(originPicker);
    const destination = selected(destinationPicker);
    return {
      originSiteId: origin.siteId,
      destination: destinationPayload(destination),
      startsAt: schedule.startsAt(),
      endsAt: schedule.endsAt(),
      tripPattern: schedule.tripPattern(),
      vehicleUse: elements.vehicleUse.value,
      carAssumptions: {
        fuelType: elements.fuelType.value,
        efficiencyKmPerLiter: Number(elements.efficiency.value),
        parkingCostKrw: Number(elements.parkingCost.value),
      },
      hasOtherLocalTripsToday: elements.otherTrips.checked,
      previousAllowanceKrw: elements.otherTrips.checked
        ? Number(elements.previousAllowance.value)
        : 0,
    };
  }

  function applySettings(settings = {}, resolvedDefaultOrigin = null) {
    if (typeof settings.vehicleUse === "string") elements.vehicleUse.value = settings.vehicleUse;
    if (typeof settings.fuelType === "string") elements.fuelType.value = settings.fuelType;
    if (Number.isFinite(settings.efficiencyKmPerLiter)) {
      elements.efficiency.value = String(settings.efficiencyKmPerLiter);
    }
    if (Number.isFinite(settings.parkingCostKrw)) {
      elements.parkingCost.value = String(settings.parkingCostKrw);
    }
    schedule.applyDefaults({
      durationMinutes: settings.defaultDurationMinutes,
      tripPattern: settings.defaultTripPattern,
    });
    if (resolvedDefaultOrigin) originPicker.selectResolved(resolvedDefaultOrigin);
  }

  function applyRecalculationDraft(draft = {}, resolvedOrigin = null) {
    originPicker.clear();
    destinationPicker.clear();
    schedule.applyDraft({
      endsAt: draft.endsAt,
      startsAt: draft.startsAt,
      tripPattern: draft.tripPattern,
    });
    const originResolved = Boolean(resolvedOrigin && originPicker.selectResolved(resolvedOrigin));
    const query = draft.destinationName || draft.destinationAddress || "";
    const destinationSearchStarted = Boolean(query);
    if (destinationSearchStarted) void destinationPicker.setQueryAndSearch(query);
    return { destinationSearchStarted, originResolved };
  }

  function clearResultState() {
    elements.onClearResult?.();
  }

  return { applyRecalculationDraft, applySettings, clearResultState, payload, valid };
}
