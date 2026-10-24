// garden3d: the gardener garden in 3D, drawn by Strata (WebGL 2).
//
// The page (gardener/web/garden3d.html) owns everything but the drawing: it
// polls /api/status, lays the plants out, handles the pointer and keyboard,
// and draws the labels and cards as HTML. It hands this module numbers only:
// one row of PLANT_STRIDE floats per plant, one row of BED_STRIDE per raised
// bed, an orbit camera and a clock. Every visual property of a plant comes
// from a column of gardener's own run history, as in the 2D garden; the
// mapping is the page's legend.
//
// Build: garden3d/build.sh (emsdk 6.0.11, Strata's wasm release, SHA-pinned).
#include <emscripten/emscripten.h>
#include <emscripten/html5.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <vector>

#include "strata/strata.h"

namespace {

// Plant row: x, z, health, successes, errors since success, in flight,
// allow-listed, seed (0..1), pulse (seconds since the page saw a new run; <0
// for none), selected.
constexpr int kPlantStride = 10;
// Bed row: x0, z0, x1, z1.
constexpr int kBedStride = 4;

enum Health { kUnplanted = 0, kStruggling, kWilting, kDry, kSteady, kThriving };

constexpr float kBedTop = 0.30f;
constexpr float kPi = 3.14159265f;

struct Rgb {
    float r, g, b;
};

// Linear RGB. The leaf ramp runs from vivid green to brown, as the 2D page's.
const Rgb kLeaf[6] = {
    {0.28f, 0.27f, 0.24f},  // unplanted
    {0.42f, 0.08f, 0.05f},  // struggling
    {0.30f, 0.16f, 0.05f},  // wilting
    {0.45f, 0.38f, 0.08f},  // dry
    {0.13f, 0.34f, 0.07f},  // steady
    {0.07f, 0.42f, 0.05f},  // thriving
};
const Rgb kStem[6] = {
    {0.22f, 0.20f, 0.17f}, {0.25f, 0.10f, 0.05f}, {0.22f, 0.14f, 0.05f},
    {0.30f, 0.27f, 0.08f}, {0.09f, 0.22f, 0.05f}, {0.06f, 0.26f, 0.04f},
};
const float kDroop[6] = {0.0f, 0.70f, 0.85f, 0.45f, 0.15f, 0.0f};
const Rgb kPetals[4] = {{0.85f, 0.25f, 0.45f}, {0.95f, 0.70f, 0.10f}, {0.55f, 0.30f, 0.85f}, {0.95f, 0.45f, 0.15f}};

struct Garden {
    StrataEngine* engine = nullptr;
    int width = 0, height = 0;
    std::vector<float> plants;
    std::vector<float> beds;
    std::vector<StrataRenderItem> items;
    std::vector<StrataPoint> points;
    std::vector<StrataLineVertex> lines;
    float bounds[4] = {-10, -10, 10, 10};  // garden rectangle x0 z0 x1 z1
    // Orbit camera.
    float yaw = 0.7f, pitch = 0.55f, dist = 30.0f, target[3] = {0, 0, 0};
    float viewProj[16] = {};
    float eye[3] = {};
    // Sun.
    float toSun[3] = {0.3f, 0.8f, 0.4f};
    float night = 0.0f;  // 0 by day .. 1 at night
    int quality = 2;     // see g3_set_quality
    bool reducedMotion = false;
};

Garden g;

void add(int kind, float x, float y, float z, float sx, float sy, float sz, Rgb c) {
    StrataRenderItem it{};
    it.kind = kind;
    it.position[0] = x;
    it.position[1] = y;
    it.position[2] = z;
    it.scale[0] = sx;
    it.scale[1] = sy;
    it.scale[2] = sz;
    it.color[0] = c.r;
    it.color[1] = c.g;
    it.color[2] = c.b;
    it.color[3] = 1.0f;
    g.items.push_back(it);
}

void point(float x, float y, float z, float px, float world, float r, float gg, float b, float a, bool soft) {
    StrataPoint p{};
    p.position[0] = x;
    p.position[1] = y;
    p.position[2] = z;
    p.size_pixels = px;
    p.world_diameter = world;
    p.color[0] = r;
    p.color[1] = gg;
    p.color[2] = b;
    p.color[3] = a;
    p.flags = soft ? STRATA_POINT_SOFT : 0u;
    g.points.push_back(p);
}

float hash(float a, float b) {
    const float h = std::sin(a * 127.1f + b * 311.7f) * 43758.5453f;
    return h - std::floor(h);
}

float plantHeight(float successes) { return std::clamp(0.35f + 0.32f * std::log2(1.0f + successes), 0.35f, 2.6f); }

void buildPlant(const float* p, double t) {
    const float x = p[0], z = p[1];
    const int health = std::clamp(static_cast<int>(p[2]), 0, 5);
    const float successes = p[3], weeds = p[4];
    const bool inFlight = p[5] > 0.5f, allowListed = p[6] > 0.5f;
    const float seed = p[7], pulse = p[8];
    const bool selected = p[9] > 0.5f;

    if (health == kUnplanted) {
        // Never tended: a mound of turned soil and a bare marker stick.
        add(STRATA_PRIMITIVE_SPHERE, x, kBedTop, z, 0.55f, 0.16f, 0.55f, {0.16f, 0.10f, 0.06f});
        add(STRATA_PRIMITIVE_CUBE, x + 0.18f, kBedTop + 0.22f, z, 0.04f, 0.44f, 0.04f, {0.35f, 0.27f, 0.17f});
    } else {
        const float h = plantHeight(successes);
        const float droop = kDroop[health];
        const float swayAmp = g.reducedMotion ? 0.0f : (inFlight ? 0.07f : 0.025f);
        const float sway = static_cast<float>(std::sin(t * (inFlight ? 2.6 : 1.1) + seed * 40.0)) * swayAmp;
        add(STRATA_PRIMITIVE_CYLINDER, x, kBedTop + h * 0.5f, z, 0.07f, h * 0.5f, 0.07f, kStem[health]);
        const int leaves = std::clamp(2 + static_cast<int>(successes / 5.0f), 2, 7);
        for (int i = 0; i < leaves; ++i) {
            const float f = (i + 1.0f) / (leaves + 1.0f);
            const float a = seed * 2.0f * kPi + i * 2.39996f;  // golden angle
            const float r = 0.20f + 0.10f * droop + 0.06f * (1.0f - f);
            const float y = kBedTop + h * f - droop * 0.20f * (0.5f + f);
            const float s = (0.75f + 0.35f * (1.0f - droop)) * (1.05f - 0.3f * f);
            add(STRATA_PRIMITIVE_SPHERE, x + std::cos(a) * r + sway * f, y, z + std::sin(a) * r, 0.32f * s,
                (0.10f + 0.06f * (1.0f - droop)) * s, 0.32f * s, kLeaf[health]);
        }
        // The crown nods over as the plant dries out.
        const float crownY = kBedTop + h - droop * 0.25f;
        const float crownX = x + sway + droop * 0.15f * std::cos(seed * 9.0f);
        add(STRATA_PRIMITIVE_SPHERE, crownX, crownY, z, 0.26f, 0.22f, 0.26f, kLeaf[health]);
        // Allow-listed and healthy: in bloom.
        if (allowListed && health >= kSteady) {
            const Rgb petal = kPetals[static_cast<int>(seed * 4.0f) & 3];
            add(STRATA_PRIMITIVE_SPHERE, crownX, crownY + 0.17f, z, 0.30f, 0.12f, 0.30f, petal);
            add(STRATA_PRIMITIVE_SPHERE, crownX, crownY + 0.22f, z, 0.11f, 0.08f, 0.11f, {0.95f, 0.80f, 0.15f});
        }
    }

    // Errors since the last success come up as weeds around the stem.
    const int weedCount = std::min(static_cast<int>(weeds), 5);
    for (int i = 0; i < weedCount; ++i) {
        const float a = seed * 17.0f + i * 1.9f;
        const float r = 0.36f + 0.05f * hash(seed, i);
        const float wh = 0.18f + 0.12f * hash(i, seed);
        add(STRATA_PRIMITIVE_CUBE, x + std::cos(a) * r, kBedTop + wh * 0.5f, z + std::sin(a) * r, 0.05f, wh, 0.05f,
            {0.20f, 0.17f, 0.04f});
        add(STRATA_PRIMITIVE_SPHERE, x + std::cos(a) * r, kBedTop + wh, z + std::sin(a) * r, 0.10f, 0.07f, 0.10f,
            {0.24f, 0.22f, 0.05f});
    }

    const float top = kBedTop + (health == kUnplanted ? 0.3f : plantHeight(successes));
    if (inFlight) {
        // Being tended right now: a lantern above it and water falling.
        point(x, top + 1.5f, z, 10.0f, 0.35f, 1.0f, 0.85f, 0.45f, 1.0f, true);
        const int drops = 8;
        for (int i = 0; i < drops; ++i) {
            const double phase = std::fmod(t * 0.9 + i / static_cast<double>(drops) + seed, 1.0);
            const float dx = (hash(seed, i * 3.0f) - 0.5f) * 0.6f, dz = (hash(i * 5.0f, seed) - 0.5f) * 0.6f;
            const float y = top + 1.3f - static_cast<float>(phase) * 1.3f;
            point(x + dx, y, z + dz, 4.0f, 0.06f, 0.45f, 0.70f, 1.0f, 0.85f, true);
        }
    }
    if (pulse >= 0.0f && pulse < 4.0f) {
        // A run just finished: a ring that spreads out and fades.
        const float r = 0.3f + pulse * 0.6f, alpha = 1.0f - pulse / 4.0f;
        for (int i = 0; i < 18; ++i) {
            const float a = i * 2.0f * kPi / 18.0f;
            point(x + std::cos(a) * r, kBedTop + 0.05f, z + std::sin(a) * r, 5.0f, 0.0f, 0.75f, 1.0f, 0.55f, alpha,
                  true);
        }
    }
    if (selected) {
        const double spin = g.reducedMotion ? 0.0 : t * 0.8;
        for (int i = 0; i < 24; ++i) {
            const float a0 = static_cast<float>(i * 2.0 * kPi / 24.0 + spin);
            const float a1 = static_cast<float>((i + 0.6) * 2.0 * kPi / 24.0 + spin);
            const float r = 0.62f, y = kBedTop + 0.03f;
            StrataLineVertex v0{{x + std::cos(a0) * r, y, z + std::sin(a0) * r}, {1.0f, 1.0f, 1.0f, 0.95f}};
            StrataLineVertex v1{{x + std::cos(a1) * r, y, z + std::sin(a1) * r}, {1.0f, 1.0f, 1.0f, 0.95f}};
            g.lines.push_back(v0);
            g.lines.push_back(v1);
        }
    }
}

void buildBeds() {
    const Rgb soil{0.11f, 0.07f, 0.04f}, wood{0.30f, 0.18f, 0.09f};
    for (size_t i = 0; i + kBedStride <= g.beds.size(); i += kBedStride) {
        const float x0 = g.beds[i], z0 = g.beds[i + 1], x1 = g.beds[i + 2], z1 = g.beds[i + 3];
        const float cx = (x0 + x1) * 0.5f, cz = (z0 + z1) * 0.5f, w = x1 - x0, d = z1 - z0;
        add(STRATA_PRIMITIVE_CUBE, cx, kBedTop * 0.5f, cz, w, kBedTop, d, soil);
        const float t = 0.12f, h = kBedTop + 0.12f;
        add(STRATA_PRIMITIVE_CUBE, cx, h * 0.5f, z0 - t * 0.5f, w + 2 * t, h, t, wood);
        add(STRATA_PRIMITIVE_CUBE, cx, h * 0.5f, z1 + t * 0.5f, w + 2 * t, h, t, wood);
        add(STRATA_PRIMITIVE_CUBE, x0 - t * 0.5f, h * 0.5f, cz, t, h, d, wood);
        add(STRATA_PRIMITIVE_CUBE, x1 + t * 0.5f, h * 0.5f, cz, t, h, d, wood);
    }
}

// The fence, the trees and the lantern posts: static, but cheap enough to
// resubmit every frame with the plants.
void buildSurroundings(std::vector<StrataPointLight>& lights) {
    const float m = 1.6f;
    const float x0 = g.bounds[0] - m, z0 = g.bounds[1] - m, x1 = g.bounds[2] + m, z1 = g.bounds[3] + m;
    const Rgb post{0.36f, 0.25f, 0.15f}, rail{0.42f, 0.30f, 0.18f};
    auto side = [&](float ax, float az, float bx, float bz, bool gate) {
        const float len = std::hypot(bx - ax, bz - az);
        const int n = std::max(1, static_cast<int>(len / 2.0f));
        for (int i = 0; i <= n; ++i) {
            const float f = static_cast<float>(i) / n;
            add(STRATA_PRIMITIVE_CUBE, ax + (bx - ax) * f, 0.5f, az + (bz - az) * f, 0.14f, 1.0f, 0.14f, post);
        }
        const bool alongX = std::fabs(bx - ax) > std::fabs(bz - az);
        for (float y : {0.45f, 0.85f}) {
            if (gate) {
                // A gap in the middle of the near side to walk in through.
                const float mid = alongX ? (ax + bx) * 0.5f : (az + bz) * 0.5f, half = 1.2f;
                const float a = alongX ? ax : az, b = alongX ? bx : bz;
                const float l0 = (mid - half) - a, l1 = b - (mid + half);
                if (alongX) {
                    add(STRATA_PRIMITIVE_CUBE, a + l0 * 0.5f, y, az, l0, 0.07f, 0.05f, rail);
                    add(STRATA_PRIMITIVE_CUBE, b - l1 * 0.5f, y, az, l1, 0.07f, 0.05f, rail);
                }
            } else if (alongX) {
                add(STRATA_PRIMITIVE_CUBE, (ax + bx) * 0.5f, y, az, len, 0.07f, 0.05f, rail);
            } else {
                add(STRATA_PRIMITIVE_CUBE, ax, y, (az + bz) * 0.5f, 0.05f, 0.07f, len, rail);
            }
        }
    };
    side(x0, z0, x1, z0, false);
    side(x0, z1, x1, z1, true);
    side(x0, z0, x0, z1, false);
    side(x1, z0, x1, z1, false);

    // Trees outside the fence, placed by a fixed hash so they never move.
    const float w = x1 - x0, d = z1 - z0;
    for (int i = 0; i < 14; ++i) {
        const float a = hash(i, 3.0f) * 2.0f * kPi;
        const float rx = (w * 0.5f + 4.0f + hash(i, 7.0f) * 9.0f), rz = (d * 0.5f + 4.0f + hash(i, 11.0f) * 9.0f);
        const float tx = (x0 + x1) * 0.5f + std::cos(a) * rx, tz = (z0 + z1) * 0.5f + std::sin(a) * rz;
        const float th = 1.6f + hash(i, 5.0f) * 1.6f, cr = 1.3f + hash(i, 9.0f) * 1.0f;
        add(STRATA_PRIMITIVE_CYLINDER, tx, th * 0.5f, tz, 0.35f, th * 0.5f, 0.35f, {0.22f, 0.13f, 0.07f});
        add(STRATA_PRIMITIVE_SPHERE, tx, th + cr * 0.6f, tz, cr * 2.0f, cr * 1.8f, cr * 2.0f, {0.05f, 0.20f, 0.05f});
        add(STRATA_PRIMITIVE_SPHERE, tx + 0.5f, th + cr * 1.2f, tz - 0.3f, cr * 1.3f, cr * 1.2f, cr * 1.3f,
            {0.06f, 0.24f, 0.06f});
    }

    // Lantern posts at the fence corners and midpoints, lit after dusk.
    const float lx[8] = {x0, (x0 + x1) * 0.5f, x1, x1, x1, (x0 + x1) * 0.5f, x0, x0};
    const float lz[8] = {z0, z0, z0, (z0 + z1) * 0.5f, z1, z1, z1, (z0 + z1) * 0.5f};
    for (int i = 0; i < 8; ++i) {
        add(STRATA_PRIMITIVE_CUBE, lx[i], 1.1f, lz[i], 0.12f, 2.2f, 0.12f, {0.10f, 0.10f, 0.11f});
        const float glow = 0.25f + 0.75f * g.night;
        add(STRATA_PRIMITIVE_SPHERE, lx[i], 2.3f, lz[i], 0.32f, 0.32f, 0.32f,
            {0.9f * glow, 0.65f * glow, 0.30f * glow});
        if (g.night > 0.02f)
            lights.push_back({{lx[i], 2.3f, lz[i]}, std::max(w, d) * 0.45f, {1.0f, 0.72f, 0.40f}, 1.4f * g.night, 0u});
    }
}

void mul4(const float* a, const float* b, float* out) {  // column-major a * b
    for (int c = 0; c < 4; ++c)
        for (int r = 0; r < 4; ++r) {
            float s = 0.0f;
            for (int k = 0; k < 4; ++k) s += a[k * 4 + r] * b[c * 4 + k];
            out[c * 4 + r] = s;
        }
}

void setTerrain() {
    // Grass around the garden, gravel paths inside the fence. Flat inside,
    // rolling a little further out.
    const float margin = 26.0f, cell = 0.5f;
    const float x0 = g.bounds[0] - margin, z0 = g.bounds[1] - margin;
    const int sx = std::clamp(static_cast<int>((g.bounds[2] - g.bounds[0] + 2 * margin) / cell) + 1, 2, 1025);
    const int sz = std::clamp(static_cast<int>((g.bounds[3] - g.bounds[1] + 2 * margin) / cell) + 1, 2, 1025);
    std::vector<float> heights(static_cast<size_t>(sx) * sz), colors(static_cast<size_t>(sx) * sz * 3);
    for (int j = 0; j < sz; ++j)
        for (int i = 0; i < sx; ++i) {
            const float x = x0 + i * cell, z = z0 + j * cell;
            const float ox = std::max({g.bounds[0] - 2.5f - x, x - g.bounds[2] - 2.5f, 0.0f});
            const float oz = std::max({g.bounds[1] - 2.5f - z, z - g.bounds[3] - 2.5f, 0.0f});
            const float out = std::hypot(ox, oz);
            const float roll = (std::sin(x * 0.21f) * std::cos(z * 0.17f) + 0.5f * std::sin(x * 0.07f + z * 0.11f));
            const size_t k = static_cast<size_t>(j) * sx + i;
            heights[k] = std::min(out * 0.12f, 1.0f) * (0.6f + 0.5f * roll) * std::min(out / 6.0f, 1.0f) * 2.0f;
            const bool inside = x > g.bounds[0] - 1.6f && x < g.bounds[2] + 1.6f && z > g.bounds[1] - 1.6f &&
                                z < g.bounds[3] + 1.6f;
            const float n = hash(static_cast<float>(i), static_cast<float>(j));
            Rgb c = inside ? Rgb{0.30f + 0.04f * n, 0.27f + 0.04f * n, 0.22f + 0.03f * n}
                           : Rgb{0.10f + 0.04f * n, 0.24f + 0.07f * n, 0.06f + 0.02f * n};
            colors[k * 3] = c.r;
            colors[k * 3 + 1] = c.g;
            colors[k * 3 + 2] = c.b;
        }
    StrataTerrainTile tile{};
    tile.origin[0] = x0;
    tile.origin[1] = -0.01f;
    tile.origin[2] = z0;
    tile.cell_size = cell;
    tile.samples_x = sx;
    tile.samples_z = sz;
    tile.heights = heights.data();
    tile.colors = colors.data();
    tile.color[3] = 1.0f;
    strata_set_terrain(g.engine, &tile);
}

void* noProcAddress(const char*, void*) { return nullptr; }

}  // namespace

extern "C" {

// Creates the WebGL 2 context on the canvas and the engine in it. Returns 0
// on success, or a negative number (no WebGL 2) or a StrataStatus.
EMSCRIPTEN_KEEPALIVE int g3_init(const char* canvasSelector, int width, int height, int quality, int reducedMotion) {
    EmscriptenWebGLContextAttributes attrs;
    emscripten_webgl_init_context_attributes(&attrs);
    attrs.majorVersion = 2;
    attrs.minorVersion = 0;
    attrs.alpha = false;
    attrs.depth = true;
    attrs.stencil = false;
    attrs.antialias = true;
    const EMSCRIPTEN_WEBGL_CONTEXT_HANDLE ctx = emscripten_webgl_create_context(canvasSelector, &attrs);
    if (ctx <= 0) return -1;
    emscripten_webgl_make_context_current(ctx);

    StrataEngineDesc desc{};
    desc.struct_size = sizeof desc;
    desc.abi_version = STRATA_ABI_VERSION;
    desc.surface = STRATA_SURFACE_HOST_CONTEXT;
    desc.width = width;
    desc.height = height;
    desc.get_proc_address = noProcAddress;  // must be non-NULL; never called in a browser
    const StrataStatus status = strata_create(&desc, &g.engine);
    if (status != STRATA_OK) {
        std::fprintf(stderr, "strata_create failed: %s (%s)\n", strata_status_string(status), strata_last_error());
        return status;
    }
    g.width = width;
    g.height = height;
    g.quality = quality;
    g.reducedMotion = reducedMotion != 0;
    std::printf("garden3d: Strata %s on %s\n", strata_version_string(), strata_gpu_description(g.engine));
    return 0;
}

// 0: no shadows (software rendering, weak phones), 1: one small cascade,
// 2: the full three. The page steps down when frames run slow.
EMSCRIPTEN_KEEPALIVE void g3_set_quality(int quality) { g.quality = std::clamp(quality, 0, 2); }

EMSCRIPTEN_KEEPALIVE void g3_resize(int width, int height) {
    if (!g.engine || width <= 0 || height <= 0) return;
    g.width = width;
    g.height = height;
    strata_resize(g.engine, width, height);
}

EMSCRIPTEN_KEEPALIVE void g3_set_beds(const float* rows, int count, float x0, float z0, float x1, float z1) {
    g.beds.assign(rows, rows + static_cast<size_t>(count) * kBedStride);
    g.bounds[0] = x0;
    g.bounds[1] = z0;
    g.bounds[2] = x1;
    g.bounds[3] = z1;
    if (g.engine) setTerrain();
}

EMSCRIPTEN_KEEPALIVE void g3_set_plants(const float* rows, int count) {
    g.plants.assign(rows, rows + static_cast<size_t>(count) * kPlantStride);
}

EMSCRIPTEN_KEEPALIVE void g3_set_camera(float yaw, float pitch, float dist, float tx, float ty, float tz) {
    g.yaw = yaw;
    g.pitch = pitch;
    g.dist = dist;
    g.target[0] = tx;
    g.target[1] = ty;
    g.target[2] = tz;
}

// The sun as it really stands over (lat, lon) at unix_seconds.
EMSCRIPTEN_KEEPALIVE float g3_set_sun(double unixSeconds, double lat, double lon) {
    StrataSolarPosition sun{};
    if (strata_solar_position(unixSeconds, lat, lon, &sun) != STRATA_OK) return 0.0f;
    std::copy(sun.to_sun, sun.to_sun + 3, g.toSun);
    g.night = std::clamp((2.0f - sun.elevation_degrees) / 10.0f, 0.0f, 1.0f);
    if (g.engine) strata_set_sun_direction(g.engine, g.toSun);
    return sun.elevation_degrees;
}

// The view-projection matrix of the last frame (column-major), for the page
// to place labels and pick plants with.
EMSCRIPTEN_KEEPALIVE const float* g3_view_proj() { return g.viewProj; }

EMSCRIPTEN_KEEPALIVE int g3_frame(double seconds) {
    if (!g.engine || g.width <= 0 || g.height <= 0) return 0;

    const float cp = std::cos(g.pitch);
    g.eye[0] = g.target[0] + g.dist * cp * std::cos(g.yaw);
    g.eye[1] = g.target[1] + g.dist * std::sin(g.pitch);
    g.eye[2] = g.target[2] + g.dist * cp * std::sin(g.yaw);
    const float up[3] = {0.0f, 1.0f, 0.0f};
    StrataCamera camera{};
    strata_camera_look_at(g.eye, g.target, up, 50.0f, static_cast<float>(g.width) / g.height, 0.3f, 400.0f, &camera);
    strata_set_camera(g.engine, &camera);
    mul4(camera.projection, camera.view, g.viewProj);

    StrataLighting lighting{};
    strata_lighting_defaults(STRATA_LIGHTING_SUN, &lighting);
    lighting.cascade_count = g.quality == 2 ? 3 : g.quality;
    lighting.shadow_map_size = g.quality == 2 ? 2048 : 1024;
    lighting.shadow_distance = std::max(40.0f, g.dist * 2.2f);
    lighting.fog = 1;
    strata_set_lighting(g.engine, &lighting);

    g.items.clear();
    g.points.clear();
    g.lines.clear();
    std::vector<StrataPointLight> lights;
    buildBeds();
    buildSurroundings(lights);
    for (size_t i = 0; i + kPlantStride <= g.plants.size(); i += kPlantStride) {
        const float* p = &g.plants[i];
        buildPlant(p, seconds);
        if (p[5] > 0.5f && lights.size() < STRATA_MAX_POINT_LIGHTS) {
            const float top = kBedTop + plantHeight(p[3]) + 1.5f;
            lights.push_back({{p[0], top, p[1]}, 6.0f, {1.0f, 0.85f, 0.5f}, 1.2f + 2.0f * g.night, 0u});
        }
    }
    if (lights.size() > STRATA_MAX_POINT_LIGHTS) lights.resize(STRATA_MAX_POINT_LIGHTS);
    strata_set_point_lights(g.engine, lights.data(), static_cast<int32_t>(lights.size()));

    StrataFrameDesc desc{};
    desc.clear_flags = STRATA_CLEAR_COLOR | STRATA_CLEAR_DEPTH | STRATA_CLEAR_SKY;
    desc.clear_color[3] = 1.0f;
    if (strata_begin_frame(g.engine, &desc) != STRATA_OK) {
        std::fprintf(stderr, "strata_begin_frame: %s\n", strata_last_error());
        return 0;
    }
    strata_submit_primitives(g.engine, g.items.data(), static_cast<int32_t>(g.items.size()));
    if (!g.lines.empty()) strata_submit_lines(g.engine, g.lines.data(), static_cast<int32_t>(g.lines.size()));
    if (!g.points.empty()) strata_submit_points(g.engine, g.points.data(), static_cast<int32_t>(g.points.size()));
    if (strata_end_frame(g.engine) != STRATA_OK) {
        std::fprintf(stderr, "strata_end_frame: %s\n", strata_last_error());
        return 0;
    }
    strata_present(g.engine);
    return static_cast<int>(g.items.size());
}

}  // extern "C"
