import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page, Route } from "@playwright/test";

const fixtureDir = join(dirname(fileURLToPath(import.meta.url)), "fixtures");

export type MockApiOptions = {
  bootstrapStatus?: number;
  facets?: object;
  facetsHang?: boolean;
  facetsStatus?: number;
  institutions?: {
    items: Array<Record<string, unknown>>;
    total?: number;
    nextOffset?: number | null;
    snapshotId?: string;
  };
  places?: object;
  preview?: object;
  previewForPayload?: (
    payload: Record<string, unknown>,
  ) => object | Promise<object>;
  reverse?: object;
};

type MapEvent = {
  id?: number;
  kind: string;
  map?: "map" | null;
  options?: Record<string, unknown>;
  point?: { latitude: number; longitude: number };
  type: string;
};

type MapState = {
  created: Array<{
    id: number;
    kind: string;
    options: Record<string, unknown>;
  }>;
  events: MapEvent[];
};

export function readFixture<T extends object>(name: string): T {
  return JSON.parse(readFileSync(join(fixtureDir, name), "utf8")) as T;
}

export async function fulfillJson(
  route: Route,
  payload: object,
  status = 200,
): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

export async function installMockApi(
  page: Page,
  options: MockApiOptions = {},
): Promise<void> {
  await page.addInitScript(() => {
    const state = {
      created: [] as Array<{
        id: number;
        kind: string;
        options: Record<string, unknown>;
      }>,
      events: [] as Array<{
        id?: number;
        kind: string;
        map?: "map" | null;
        options?: Record<string, unknown>;
        point?: { latitude: number; longitude: number };
        type: string;
      }>,
      nextOverlayId: 1,
      clickHandler: null as ((event: { latLng: LatLngFake }) => Promise<void> | void) | null,
      async triggerClick(latitude: number, longitude: number): Promise<void> {
        await this.clickHandler?.({ latLng: new LatLngFake(latitude, longitude) });
      },
    };

    class MapFake {
      setBounds(): void {
        state.events.push({ kind: "Map", type: "setBounds" });
      }

      panTo(point: LatLngFake): void {
        state.events.push({
          kind: "Map",
          point: { latitude: point.lat, longitude: point.lng },
          type: "panTo",
        });
      }
    }

    class OverlayFake {
      id: number;
      kind: string;

      constructor(options: Record<string, unknown> = {}) {
        this.id = state.nextOverlayId++;
        this.kind = this.constructor.name;
        state.created.push({ id: this.id, kind: this.kind, options });
      }

      setMap(map: unknown): void {
        state.events.push({
          id: this.id,
          kind: this.kind,
          map: map === null ? null : "map",
          type: "setMap",
        });
      }

      setOptions(options: Record<string, unknown>): void {
        state.events.push({
          id: this.id,
          kind: this.kind,
          options,
          type: "setOptions",
        });
      }
    }

    class MarkerFake extends OverlayFake {}
    class PolylineFake extends OverlayFake {}
    class PolygonFake extends OverlayFake {}

    class LatLngFake {
      constructor(
        public lat: number,
        public lng: number,
      ) {}

      getLat(): number {
        return this.lat;
      }

      getLng(): number {
        return this.lng;
      }
    }

    class BoundsFake {
      extend(): void {
        state.events.push({ kind: "LatLngBounds", type: "extend" });
      }
    }

    Object.assign(window, {
      __task8Map: state,
      kakao: {
        maps: {
          load: (callback: () => void) => callback(),
          Map: MapFake,
          Marker: MarkerFake,
          Polyline: PolylineFake,
          Polygon: PolygonFake,
          LatLng: LatLngFake,
          LatLngBounds: BoundsFake,
          MapTypeId: { ROADMAP: "ROADMAP" },
          event: {
            addListener: (
              _map: MapFake,
              eventName: string,
              callback: (event: { latLng: LatLngFake }) => Promise<void> | void,
            ) => {
              if (eventName === "click") state.clickHandler = callback;
            },
          },
        },
      },
    });
  });
  await page.route("**/api/v1/**", async (route) => {
    const requestUrl = new URL(route.request().url());
    const path = requestUrl.pathname;
    if (path === "/api/v1/bootstrap") {
      return fulfillJson(
        route,
        options.bootstrapStatus
          ? { error: { code: "BOOTSTRAP_UNAVAILABLE" } }
          : readFixture("bootstrap.json"),
        options.bootstrapStatus ?? 200,
      );
    }
    if (path === "/api/v1/institutions/facets") {
      if (options.facetsHang) return;
      return fulfillJson(
        route,
        options.facetsStatus
          ? { error: { code: "FACETS_UNAVAILABLE" } }
          : options.facets ?? readFixture("institution-facets.json"),
        options.facetsStatus ?? 200,
      );
    }
    if (path === "/api/v1/institutions") {
      const payload = options.institutions ?? readFixture<{
        items: Array<Record<string, unknown>>;
        total: number;
        nextOffset: number | null;
        snapshotId: string;
      }>("institutions.json");
      const items = payload.items.filter((item) => (
        (!requestUrl.searchParams.get("institution_type")
          || item.institutionType === requestUrl.searchParams.get("institution_type"))
        && (!requestUrl.searchParams.get("foundation_type")
          || item.foundationType === requestUrl.searchParams.get("foundation_type"))
        && (!requestUrl.searchParams.get("education_office")
          || item.educationOffice === requestUrl.searchParams.get("education_office"))
        && (!requestUrl.searchParams.get("district")
          || item.district === requestUrl.searchParams.get("district"))
      ));
      return fulfillJson(route, {
        items,
        total: payload.total ?? items.length,
        nextOffset: payload.nextOffset ?? null,
        snapshotId: payload.snapshotId ?? "fixture-001",
      });
    }
    if (path === "/api/v1/places/reverse") {
      return fulfillJson(
        route,
        options.reverse ?? readFixture("reverse.json"),
      );
    }
    if (path === "/api/v1/places") {
      return fulfillJson(
        route,
        options.places ?? readFixture("places.json"),
      );
    }
    if (path === "/api/v1/geodata/seoul") {
      return fulfillJson(route, readFixture("seoul.geojson"));
    }
    if (path === "/api/v1/geodata/support") {
      return fulfillJson(route, readFixture("support.geojson"));
    }
    if (path === "/api/v1/trips/preview") {
      const payload = JSON.parse(route.request().postData() || "{}") as Record<string, unknown>;
      return fulfillJson(
        route,
        await options.previewForPayload?.(payload)
          ?? options.preview
          ?? readFixture("preview.json"),
      );
    }
    return route.fallback();
  });
}

export async function mapState(page: Page): Promise<MapState> {
  return page.evaluate(() => {
    const task8Window = window as typeof window & {
      __task8Map: MapState;
    };
    return {
      created: task8Window.__task8Map.created,
      events: task8Window.__task8Map.events,
    };
  });
}

export async function triggerMapClick(
  page: Page,
  latitude = 37.5663,
  longitude = 126.9779,
): Promise<void> {
  await page.evaluate(
    async ({ latitude: pointLatitude, longitude: pointLongitude }) => {
      const task8Window = window as typeof window & {
        __task8Map: {
          triggerClick(latitude: number, longitude: number): Promise<void>;
        };
      };
      await task8Window.__task8Map.triggerClick(pointLatitude, pointLongitude);
    },
    { latitude, longitude },
  );
}

export async function completePublicOfficialTrip(page: Page): Promise<void> {
  await page.getByLabel("출발 기관").fill("샘물");
  await page
    .getByRole("option", { name: /샘물공립초등학교.*공립.*중구/ })
    .click();
  await page.getByLabel("출장지").fill("서울시청");
  await page
    .getByRole("option", { name: /서울특별시청.*세종대로 110/ })
    .click();
  await page.getByRole("button", { name: "경로 계산" }).click();
}
