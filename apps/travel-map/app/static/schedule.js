const DATE_VALUE = /^(\d{4})-(\d{2})-(\d{2})$/;
const TIME_VALUE = /^(\d{2}):(\d{2})$/;
const MIN_DURATION = 2;
const MAX_DURATION = 24 * 60;

function parsedDate(dateValue) {
  const match = DATE_VALUE.exec(dateValue || "");
  if (!match) return null;
  const [year, month, day] = match.slice(1).map(Number);
  const value = new Date(Date.UTC(year, month - 1, day));
  if (
    value.getUTCFullYear() !== year
    || value.getUTCMonth() !== month - 1
    || value.getUTCDate() !== day
  ) return null;
  return { day, month, year };
}

function parsedTime(timeValue) {
  const match = TIME_VALUE.exec(timeValue || "");
  if (!match) return null;
  const [hour, minute] = match.slice(1).map(Number);
  if (hour > 23 || minute > 59) return null;
  return { hour, minute };
}

function pad(value) {
  return String(value).padStart(2, "0");
}

function setInputValue(input, value) {
  if (input.value !== String(value)) input.value = String(value);
}

export function addLocalMinutes(dateValue, timeValue, minutes) {
  const date = parsedDate(dateValue);
  const time = parsedTime(timeValue);
  if (!date || !time || !Number.isInteger(minutes)) return null;
  const shifted = new Date(Date.UTC(
    date.year,
    date.month - 1,
    date.day,
    time.hour,
    time.minute + minutes,
  ));
  return {
    date: `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}`,
    time: `${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}`,
  };
}

export function differenceLocalMinutes(startDate, startTime, endDate, endTime) {
  const startDay = parsedDate(startDate);
  const startClock = parsedTime(startTime);
  const endDay = parsedDate(endDate);
  const endClock = parsedTime(endTime);
  if (!startDay || !startClock || !endDay || !endClock) return null;
  const start = Date.UTC(
    startDay.year,
    startDay.month - 1,
    startDay.day,
    startClock.hour,
    startClock.minute,
  );
  const end = Date.UTC(
    endDay.year,
    endDay.month - 1,
    endDay.day,
    endClock.hour,
    endClock.minute,
  );
  return Math.round((end - start) / 60_000);
}

export function createScheduleController({ elements, onValidityChange = () => {} }) {
  const {
    endDate,
    endTime,
    quickButtons = [],
    startDate,
    startTime,
    durationHours,
    durationMinutes,
    tripPattern,
  } = elements;
  let destroyed = false;

  function durationMinutesValue() {
    const hours = Number(durationHours.value);
    const minutes = Number(durationMinutes.value);
    if (!Number.isInteger(hours) || !Number.isInteger(minutes) || hours < 0 || minutes < 0 || minutes > 59) {
      return null;
    }
    return hours * 60 + minutes;
  }

  function writeDuration(minutes) {
    const safeMinutes = Number.isInteger(minutes) && minutes >= 0 ? minutes : 0;
    setInputValue(durationHours, Math.floor(safeMinutes / 60));
    setInputValue(durationMinutes, safeMinutes % 60);
  }

  function notify() {
    onValidityChange(valid());
  }

  function updateEndFromDuration() {
    const duration = durationMinutesValue();
    if (duration == null) return notify();
    const end = addLocalMinutes(startDate.value, startTime.value, duration);
    if (end) {
      endDate.value = end.date;
      endTime.value = end.time;
    }
    notify();
  }

  function updateDurationFromEnd() {
    writeDuration(differenceLocalMinutes(
      startDate.value,
      startTime.value,
      endDate.value,
      endTime.value,
    ));
    notify();
  }

  function handleStartChange() {
    updateEndFromDuration();
  }

  function handleDurationChange() {
    updateEndFromDuration();
  }

  function handleEndChange() {
    updateDurationFromEnd();
  }

  function handlePatternChange() {
    notify();
  }

  function selectQuickDuration(event) {
    const hours = Number(event.currentTarget.dataset.durationHours);
    if (!Number.isInteger(hours)) return;
    writeDuration(hours * 60);
    updateEndFromDuration();
  }

  function startsAt() {
    return parsedDate(startDate.value) && parsedTime(startTime.value)
      ? `${startDate.value}T${startTime.value}:00+09:00`
      : null;
  }

  function endsAt() {
    return parsedDate(endDate.value) && parsedTime(endTime.value)
      ? `${endDate.value}T${endTime.value}:00+09:00`
      : null;
  }

  function tripPatternValue() {
    return [...tripPattern].find((input) => input.checked)?.value || null;
  }

  function valid() {
    const duration = durationMinutesValue();
    const difference = differenceLocalMinutes(
      startDate.value,
      startTime.value,
      endDate.value,
      endTime.value,
    );
    return Boolean(
      startsAt()
      && endsAt()
      && tripPatternValue()
      && duration != null
      && duration >= MIN_DURATION
      && duration <= MAX_DURATION
      && difference === duration,
    );
  }

  function applyDefaults({ tripPattern: pattern, durationMinutes: duration } = {}) {
    if (typeof pattern === "string") {
      const match = [...tripPattern].find((input) => input.value === pattern);
      if (match) match.checked = true;
    }
    if (Number.isInteger(duration)) writeDuration(duration);
    updateEndFromDuration();
  }

  function applyDraft({ startsAt: start, endsAt: end, tripPattern: pattern } = {}) {
    const startMatch = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(start || "");
    const endMatch = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(end || "");
    if (startMatch) {
      startDate.value = startMatch[1];
      startTime.value = startMatch[2];
    }
    if (endMatch) {
      endDate.value = endMatch[1];
      endTime.value = endMatch[2];
      updateDurationFromEnd();
    } else {
      updateEndFromDuration();
    }
    if (typeof pattern === "string") {
      const match = [...tripPattern].find((input) => input.value === pattern);
      if (match) match.checked = true;
    }
    notify();
  }

  startDate.addEventListener("change", handleStartChange);
  startTime.addEventListener("change", handleStartChange);
  startDate.addEventListener("input", handleStartChange);
  startTime.addEventListener("input", handleStartChange);
  durationHours.addEventListener("change", handleDurationChange);
  durationMinutes.addEventListener("change", handleDurationChange);
  durationHours.addEventListener("input", handleDurationChange);
  durationMinutes.addEventListener("input", handleDurationChange);
  endDate.addEventListener("change", handleEndChange);
  endTime.addEventListener("change", handleEndChange);
  endDate.addEventListener("input", handleEndChange);
  endTime.addEventListener("input", handleEndChange);
  tripPattern.forEach((input) => input.addEventListener("change", handlePatternChange));
  quickButtons.forEach((button) => button.addEventListener("click", selectQuickDuration));
  notify();

  return {
    applyDefaults,
    applyDraft,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      startDate.removeEventListener("change", handleStartChange);
      startTime.removeEventListener("change", handleStartChange);
      startDate.removeEventListener("input", handleStartChange);
      startTime.removeEventListener("input", handleStartChange);
      durationHours.removeEventListener("change", handleDurationChange);
      durationMinutes.removeEventListener("change", handleDurationChange);
      durationHours.removeEventListener("input", handleDurationChange);
      durationMinutes.removeEventListener("input", handleDurationChange);
      endDate.removeEventListener("change", handleEndChange);
      endTime.removeEventListener("change", handleEndChange);
      endDate.removeEventListener("input", handleEndChange);
      endTime.removeEventListener("input", handleEndChange);
      tripPattern.forEach((input) => input.removeEventListener("change", handlePatternChange));
      quickButtons.forEach((button) => button.removeEventListener("click", selectQuickDuration));
    },
    durationMinutes: durationMinutesValue,
    endsAt,
    startsAt,
    tripPattern: tripPatternValue,
    valid,
  };
}
