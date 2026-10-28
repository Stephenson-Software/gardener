// fleetcity: the repository fleet as a city, drawn by Strata (WebGL 2), for
// the hub's /city page (Stephenson-Software RFC 0026).
//
// The hub lays the city out (gardener/fleetcity.py) and the page
// (gardener/web/city.html) owns the data, labels and timelapse. This module
// only draws, and answers one question: which building is under a point on
// the canvas. It is handed numbers only: one row of kBuildingStride floats
// per building, one of kDistrictStride per district, and two RGBA8 palettes
// with one texel per building.
//
// RFC 0026 §4: each district is one textured mesh for the bodies (lit) and
// one for the roofs (unlit, so a lit roof glows at night), never one mesh
// per repository (Strata holds at most 256). A per-triangle tag table maps a
// strata_raycast_meshes hit back to its building, the TriTag pattern of
// kindling's render bridge. Colours live in the two palette textures, so a
// timelapse frame is one strata_update_texture_rgba8 call and rebuilds no
// geometry.
//
// The shell around the city (the WebGL 2 context, resizing, quality tiers,
// the orbit camera, the sun, the frame and its batches, a lost context) is
// the Strata web kit's strata_kit.h, vendored in strata-kit/ at the
// repository's root and shared with the 3D garden. This file is the city: it
// uploads the meshes in strata_kit_engine_ready and fills the frame's
// batches in strata_kit_build.
//
// Build: fleetcity/build.sh (emsdk 6.0.11, Strata's wasm release, SHA-pinned).
#define STRATA_KIT_MAIN
#include "strata_kit.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <vector>

namespace {

// Building row: id, district, x0, z0, x1, z1, height.
constexpr int kBuildingStride = 7;
// District row: x0, z0, x1, z1.
constexpr int kDistrictStride = 4;
// Beacon row (a building being tended right now): x, y, z, r, g, b, strength.
constexpr int kBeaconStride = 7;
constexpr float kRoof = 0.08f;   // roof slab thickness
constexpr float kInset = 0.06f;  // roof inset from the walls
constexpr float kPad = 0.04f;    // district pavement height

struct TriTag {
    uint32_t first = 0, count = 0;
    int32_t id = -1;
};

struct MeshData {
    std::vector<float> positions, normals, colors, uvs;
    std::vector<uint32_t> indices;
    std::vector<TriTag> tags;
    uint32_t vertices() const { return static_cast<uint32_t>(positions.size() / 3); }
    uint32_t triangles() const { return static_cast<uint32_t>(indices.size() / 3); }
    void clear() {
        positions.clear(), normals.clear(), colors.clear(), uvs.clear(), indices.clear(), tags.clear();
    }
};

struct Building {
    int id;
    float x0, z0, x1, z1, h;
};

struct City {
    int texW = 32, texH = 1;
    uint32_t bodyTexture = 0, roofTexture = 0;
    std::vector<uint32_t> meshIds;
    // For each mesh id in meshIds, its tag table (empty for the ground).
    std::vector<std::vector<TriTag>> meshTags;
    std::vector<Building> buildings;
    std::vector<float> districts;
    std::vector<float> beacons;
    std::vector<float> glows;
    float bounds[4] = {-10, -10, 10, 10};
    int selected = -1, hovered = -1;
    bool citySet = false;
};

City c;

strata_kit::Host& host() { return strata_kit::host(); }
StrataEngine* engine() { return host().engine; }

void face(MeshData& m, const float p[4][3], const float n[3], const float col[3], float u, float v) {
    const uint32_t base = m.vertices();
    for (int i = 0; i < 4; ++i) {
        m.positions.insert(m.positions.end(), {p[i][0], p[i][1], p[i][2]});
        m.normals.insert(m.normals.end(), {n[0], n[1], n[2]});
        m.colors.insert(m.colors.end(), {col[0], col[1], col[2]});
        m.uvs.insert(m.uvs.end(), {u, v});
    }
    // p0..p3 wind counter-clockwise seen from the side n points to.
    m.indices.insert(m.indices.end(), {base, base + 1, base + 2, base, base + 2, base + 3});
}

// An axis-aligned box, flat shaded: 24 vertices, 12 triangles. With bottom
// false the bottom face is left out (10 triangles): it is never seen.
void box(MeshData& m, float x0, float y0, float z0, float x1, float y1, float z1, const float col[3], float u, float v,
         bool bottom) {
    const float top[4][3] = {{x0, y1, z1}, {x1, y1, z1}, {x1, y1, z0}, {x0, y1, z0}};
    const float under[4][3] = {{x0, y0, z0}, {x1, y0, z0}, {x1, y0, z1}, {x0, y0, z1}};
    const float south[4][3] = {{x0, y0, z1}, {x1, y0, z1}, {x1, y1, z1}, {x0, y1, z1}};
    const float north[4][3] = {{x1, y0, z0}, {x0, y0, z0}, {x0, y1, z0}, {x1, y1, z0}};
    const float east[4][3] = {{x1, y0, z1}, {x1, y0, z0}, {x1, y1, z0}, {x1, y1, z1}};
    const float west[4][3] = {{x0, y0, z0}, {x0, y0, z1}, {x0, y1, z1}, {x0, y1, z0}};
    const float up[3] = {0, 1, 0}, down[3] = {0, -1, 0}, s[3] = {0, 0, 1}, n[3] = {0, 0, -1}, e[3] = {1, 0, 0},
                w[3] = {-1, 0, 0};
    face(m, top, up, col, u, v);
    if (bottom) face(m, under, down, col, u, v);
    face(m, south, s, col, u, v);
    face(m, north, n, col, u, v);
    face(m, east, e, col, u, v);
    face(m, west, w, col, u, v);
}

uint32_t addMesh(MeshData& m, uint32_t texture, uint32_t flags) {
    if (m.indices.empty()) return 0;
    StrataTexturedMesh tm{};
    tm.mesh.positions = m.positions.data();
    tm.mesh.normals = m.normals.data();
    tm.mesh.colors = m.colors.data();
    tm.mesh.indices = m.indices.data();
    tm.mesh.vertex_count = static_cast<int32_t>(m.vertices());
    tm.mesh.index_count = static_cast<int32_t>(m.indices.size());
    tm.mesh.color[0] = tm.mesh.color[1] = tm.mesh.color[2] = tm.mesh.color[3] = 1.0f;
    tm.uvs = m.uvs.data();
    tm.texture = texture;
    tm.flags = flags;
    uint32_t id = 0;
    if (strata_add_textured_mesh(engine(), &tm, &id) != STRATA_OK) {
        std::fprintf(stderr, "fleetcity: strata_add_textured_mesh: %s\n", strata_last_error());
        return 0;
    }
    c.meshIds.push_back(id);
    c.meshTags.push_back(m.tags);
    return id;
}

void rebuild() {
    StrataEngine* e = engine();
    if (!e) return;
    strata_clear_meshes(e);
    c.meshIds.clear();
    c.meshTags.clear();
    if (c.bodyTexture) strata_destroy_texture(e, c.bodyTexture);
    if (c.roofTexture) strata_destroy_texture(e, c.roofTexture);
    c.bodyTexture = c.roofTexture = 0;
    std::vector<uint8_t> grey(static_cast<size_t>(c.texW) * c.texH * 4, 128);
    const uint32_t flags = STRATA_TEXTURE_NEAREST | STRATA_TEXTURE_CLAMP_U | STRATA_TEXTURE_CLAMP_V;
    strata_create_texture_rgba8(e, c.texW, c.texH, grey.data(), flags, &c.bodyTexture);
    strata_create_texture_rgba8(e, c.texW, c.texH, grey.data(), flags, &c.roofTexture);

    // The ground: asphalt around everything, a paved block per district.
    MeshData ground;
    const float asphalt[3] = {0.045f, 0.047f, 0.052f}, paving[3] = {0.16f, 0.16f, 0.15f};
    const float m = 6.0f;
    box(ground, c.bounds[0] - m, -0.2f, c.bounds[1] - m, c.bounds[2] + m, 0.0f, c.bounds[3] + m, asphalt, 0, 0, false);
    for (size_t i = 0; i + kDistrictStride <= c.districts.size(); i += kDistrictStride) {
        const float* d = &c.districts[i];
        box(ground, d[0], 0.0f, d[1], d[2], kPad, d[3], paving, 0, 0, false);
    }
    addMesh(ground, 0, 0);
}

// The district of each building, parallel to c.buildings.
std::vector<int> gDistrictOf;

// The ground, then one body mesh and one roof mesh per district, in
// district order: 1 + 2 * districts meshes, far under STRATA_MAX_MESHES.
void rebuildMeshes() {
    if (!engine()) return;
    rebuild();
    const size_t nd = c.districts.size() / kDistrictStride;
    if (!nd) return;
    std::vector<MeshData> bodies(nd), roofs(nd);
    const float white[3] = {1, 1, 1};
    for (size_t k = 0; k < c.buildings.size(); ++k) {
        const Building& b = c.buildings[k];
        const size_t d = static_cast<size_t>(std::clamp(gDistrictOf[k], 0, static_cast<int>(nd) - 1));
        // The centre of the building's texel, so filtering never reaches a
        // neighbour's colour.
        const float u = (b.id % c.texW + 0.5f) / c.texW, v = (b.id / c.texW + 0.5f) / c.texH;
        MeshData& body = bodies[d];
        TriTag t;
        t.first = body.triangles();
        t.id = b.id;
        box(body, b.x0, kPad, b.z0, b.x1, kPad + b.h, b.z1, white, u, v, false);
        t.count = body.triangles() - t.first;
        body.tags.push_back(t);
        MeshData& roof = roofs[d];
        TriTag r;
        r.first = roof.triangles();
        r.id = b.id;
        box(roof, b.x0 + kInset, kPad + b.h, b.z0 + kInset, b.x1 - kInset, kPad + b.h + kRoof, b.z1 - kInset, white,
            u, v, false);
        r.count = roof.triangles() - r.first;
        roof.tags.push_back(r);
    }
    for (size_t d = 0; d < nd; ++d) {
        addMesh(bodies[d], c.bodyTexture, 0);
        addMesh(roofs[d], c.roofTexture, STRATA_MESH_UNLIT);
    }
}

int32_t tagOf(const std::vector<TriTag>& tags, uint32_t triangle) {
    // Tags are appended in triangle order: the last one starting at or
    // before the triangle is the only candidate.
    auto it = std::upper_bound(tags.begin(), tags.end(), triangle,
                               [](uint32_t t, const TriTag& tag) { return t < tag.first; });
    if (it == tags.begin()) return -1;
    --it;
    return triangle < it->first + it->count ? it->id : -1;
}

bool invert4(const float* m, float* out) {
    float inv[16];
    inv[0] = m[5] * m[10] * m[15] - m[5] * m[11] * m[14] - m[9] * m[6] * m[15] + m[9] * m[7] * m[14] +
             m[13] * m[6] * m[11] - m[13] * m[7] * m[10];
    inv[4] = -m[4] * m[10] * m[15] + m[4] * m[11] * m[14] + m[8] * m[6] * m[15] - m[8] * m[7] * m[14] -
             m[12] * m[6] * m[11] + m[12] * m[7] * m[10];
    inv[8] = m[4] * m[9] * m[15] - m[4] * m[11] * m[13] - m[8] * m[5] * m[15] + m[8] * m[7] * m[13] +
             m[12] * m[5] * m[11] - m[12] * m[7] * m[9];
    inv[12] = -m[4] * m[9] * m[14] + m[4] * m[10] * m[13] + m[8] * m[5] * m[14] - m[8] * m[6] * m[13] -
              m[12] * m[5] * m[10] + m[12] * m[6] * m[9];
    inv[1] = -m[1] * m[10] * m[15] + m[1] * m[11] * m[14] + m[9] * m[2] * m[15] - m[9] * m[3] * m[14] -
             m[13] * m[2] * m[11] + m[13] * m[3] * m[10];
    inv[5] = m[0] * m[10] * m[15] - m[0] * m[11] * m[14] - m[8] * m[2] * m[15] + m[8] * m[3] * m[14] +
             m[12] * m[2] * m[11] - m[12] * m[3] * m[10];
    inv[9] = -m[0] * m[9] * m[15] + m[0] * m[11] * m[13] + m[8] * m[1] * m[15] - m[8] * m[3] * m[13] -
             m[12] * m[1] * m[11] + m[12] * m[3] * m[9];
    inv[13] = m[0] * m[9] * m[14] - m[0] * m[10] * m[13] - m[8] * m[1] * m[14] + m[8] * m[2] * m[13] +
              m[12] * m[1] * m[10] - m[12] * m[2] * m[9];
    inv[2] = m[1] * m[6] * m[15] - m[1] * m[7] * m[14] - m[5] * m[2] * m[15] + m[5] * m[3] * m[14] +
             m[13] * m[2] * m[7] - m[13] * m[3] * m[6];
    inv[6] = -m[0] * m[6] * m[15] + m[0] * m[7] * m[14] + m[4] * m[2] * m[15] - m[4] * m[3] * m[14] -
             m[12] * m[2] * m[7] + m[12] * m[3] * m[6];
    inv[10] = m[0] * m[5] * m[15] - m[0] * m[7] * m[13] - m[4] * m[1] * m[15] + m[4] * m[3] * m[13] +
              m[12] * m[1] * m[7] - m[12] * m[3] * m[5];
    inv[14] = -m[0] * m[5] * m[14] + m[0] * m[6] * m[13] + m[4] * m[1] * m[14] - m[4] * m[2] * m[13] -
              m[12] * m[1] * m[6] + m[12] * m[2] * m[5];
    inv[3] = -m[1] * m[6] * m[11] + m[1] * m[7] * m[10] + m[5] * m[2] * m[11] - m[5] * m[3] * m[10] -
             m[9] * m[2] * m[7] + m[9] * m[3] * m[6];
    inv[7] = m[0] * m[6] * m[11] - m[0] * m[7] * m[10] - m[4] * m[2] * m[11] + m[4] * m[3] * m[10] +
             m[8] * m[2] * m[7] - m[8] * m[3] * m[6];
    inv[11] = -m[0] * m[5] * m[11] + m[0] * m[7] * m[9] + m[4] * m[1] * m[11] - m[4] * m[3] * m[9] -
              m[8] * m[1] * m[7] + m[8] * m[3] * m[5];
    inv[15] = m[0] * m[5] * m[10] - m[0] * m[6] * m[9] - m[4] * m[1] * m[10] + m[4] * m[2] * m[9] +
              m[8] * m[1] * m[6] - m[8] * m[2] * m[5];
    float det = m[0] * inv[0] + m[1] * inv[4] + m[2] * inv[8] + m[3] * inv[12];
    if (std::fabs(det) < 1e-12f) return false;
    det = 1.0f / det;
    for (int i = 0; i < 16; ++i) out[i] = inv[i] * det;
    return true;
}

const Building* findBuilding(int id) {
    for (const auto& b : c.buildings)
        if (b.id == id) return &b;
    return nullptr;
}

void outline(const Building& b, float r, float g, float bl, float a) {
    const float pad = 0.08f;
    const float x0 = b.x0 - pad, x1 = b.x1 + pad, z0 = b.z0 - pad, z1 = b.z1 + pad;
    const float y0 = kPad + 0.01f, y1 = kPad + b.h + kRoof + pad;
    const float px[4] = {x0, x1, x1, x0}, pz[4] = {z0, z0, z1, z1};
    auto seg = [&](float ax, float ay, float az, float bx, float by, float bz) {
        host().line(ax, ay, az, bx, by, bz, {r, g, bl}, a);
    };
    for (int i = 0; i < 4; ++i) {
        const int j = (i + 1) % 4;
        seg(px[i], y0, pz[i], px[j], y0, pz[j]);
        seg(px[i], y1, pz[i], px[j], y1, pz[j]);
        seg(px[i], y0, pz[i], px[i], y1, pz[i]);
    }
}

// Street lamps at a district's four corners, outside its pavement.
template <typename F>
void eachLamp(const float* d, F f) {
    const float xs[2] = {d[0] - 0.6f, d[2] + 0.6f}, zs[2] = {d[1] - 0.6f, d[3] + 0.6f};
    for (float x : xs)
        for (float z : zs) f(x, z);
}

constexpr float kLampHeight = 3.0f;

}  // namespace

// The kit's hooks (strata_kit.h).

void strata_kit_configure(strata_kit::Options& o) {
    o.name = "fleetcity";
    o.fovDegrees = 45.0f;
    o.nearPlane = 0.5f;
    o.farPlane = 600.0f;  // raised with the camera's distance in strata_kit_build
    o.shadowDistanceMin = 60.0f;
    o.shadowDistancePerDist = 2.0f;
    // Every beacon and street lamp may be lit at every tier, as before the
    // kit: the budget is the engine's maximum, beacons first.
    for (strata_kit::Tier& t : o.tiers) t.maxLights = STRATA_MAX_POINT_LIGHTS;
}

// After the engine is made, and again after a lost WebGL context is
// restored: the meshes and the two palette textures died with the old
// engine, so their ids are dropped (never destroyed in the new engine) and
// the city is uploaded again. The page then re-sends both palettes.
void strata_kit_engine_ready(strata_kit::Host&) {
    if (strata_abi_minor() < 10)
        std::fprintf(stderr, "fleetcity: Strata ABI 1.%u is older than 1.10\n", strata_abi_minor());
    c.bodyTexture = c.roofTexture = 0;
    c.meshIds.clear();
    c.meshTags.clear();
    if (c.citySet) rebuildMeshes();
}

void strata_kit_build(strata_kit::Host& h, double seconds) {
    // A city far below the camera must not fall past the far plane. The
    // camera of this frame is already set, so this takes effect from the
    // next frame; the factor of 4 leaves room for a frame's zoom.
    h.opts.farPlane = std::max(600.0f, h.dist * 4.0f);

    // Buildings being tended right now: a beacon over the roof, pulsing.
    int beacons = 0;
    for (size_t i = 0; i + kBeaconStride <= c.beacons.size(); i += kBeaconStride) {
        const float* b = &c.beacons[i];
        const float pulse = h.reducedMotion ? 1.0f : 0.75f + 0.25f * static_cast<float>(std::sin(seconds * 3.0 + i));
        const strata_kit::Rgb col{b[3], b[4], b[5]};
        h.point(b[0], b[1] + 0.9f, b[2], 14.0f * pulse, 1.4f, col, b[6]);
        for (int k = 0; k < 6; ++k)  // a column of light down to the roof
            h.point(b[0], b[1] + 0.15f * k, b[2], 5.0f, 0.35f, col, std::clamp(b[6], 0.0f, 1.0f) * 0.6f);
        if (beacons++ < 8) h.light(b[0], b[1] + 1.0f, b[2], 7.0f, col, (1.0f + 2.0f * h.night) * pulse, 2);
    }
    // Soft glows over lit roofs, so a night's runs read at a distance where
    // a roof is a pixel.
    for (size_t i = 0; i + kBeaconStride <= c.glows.size(); i += kBeaconStride) {
        const float* g = &c.glows[i];
        const float level = std::clamp(g[6], 0.0f, 1.0f);
        h.point(g[0], g[1] + 0.25f, g[2], 4.0f + 8.0f * level, 1.4f, {g[3], g[4], g[5]}, 0.25f + 0.6f * level);
    }
    // Street lamps at the district corners: the posts always, the lamps lit
    // after dusk (below the beacons when the light budget runs out).
    for (size_t i = 0; i + kDistrictStride <= c.districts.size(); i += kDistrictStride) {
        const float* d = &c.districts[i];
        const float reach = std::max(d[2] - d[0], d[3] - d[1]) * 0.6f;
        eachLamp(d, [&](float x, float z) {
            h.add(STRATA_PRIMITIVE_CYLINDER, x, kLampHeight * 0.5f, z, 0.06f, kLampHeight * 0.5f, 0.06f,
                  {0.06f, 0.06f, 0.07f});
            if (h.night > 0.02f) {
                h.light(x, kLampHeight, z, reach, {1.0f, 0.78f, 0.5f}, 0.9f * h.night, 1);
                h.point(x, kLampHeight, z, 4.0f, 0.5f, {1.0f, 0.8f, 0.5f}, h.night);
            }
        });
    }
    if (const Building* b = findBuilding(c.hovered)) outline(*b, 0.85f, 0.9f, 1.0f, 0.55f);
    if (const Building* b = findBuilding(c.selected)) outline(*b, 1.0f, 1.0f, 1.0f, 0.95f);
}

// The page's setters. The camera, the sun, the size and the quality are the
// kit's (sk_*).

// Replaces the whole city: buildings (kBuildingStride floats each), districts
// (kDistrictStride each), the palette size and the bounds. Both palettes are
// reset to grey; fc_set_palette fills them. Returns the buildings kept.
STRATA_KIT_EXPORT int fc_set_city(const float* rows, int count, const float* districts, int districtCount, int texW,
                                  int texH, float x0, float z0, float x1, float z1) {
    if (texW < 1 || texH < 1 || texW > STRATA_MAX_TEXTURE_SIZE || texH > STRATA_MAX_TEXTURE_SIZE) return -1;
    c.texW = texW;
    c.texH = texH;
    c.bounds[0] = x0;
    c.bounds[1] = z0;
    c.bounds[2] = x1;
    c.bounds[3] = z1;
    c.districts.assign(districts, districts + static_cast<size_t>(std::max(districtCount, 0)) * kDistrictStride);
    c.buildings.clear();
    gDistrictOf.clear();
    for (int i = 0; i < count; ++i) {
        const float* r = rows + static_cast<size_t>(i) * kBuildingStride;
        const int id = static_cast<int>(r[0]);
        if (id < 0 || id >= texW * texH || !(r[6] > 0.0f) || !(r[4] > r[2]) || !(r[5] > r[3])) continue;
        c.buildings.push_back({id, r[2], r[3], r[4], r[5], r[6]});
        gDistrictOf.push_back(static_cast<int>(r[1]));
    }
    c.citySet = true;
    rebuildMeshes();
    return static_cast<int>(c.buildings.size());
}

// which 0: building bodies, 1: roofs. rgba is texW * texH * 4 bytes.
STRATA_KIT_EXPORT void fc_set_palette(int which, const uint8_t* rgba) {
    const uint32_t tex = which == 0 ? c.bodyTexture : c.roofTexture;
    if (!engine() || !tex) return;
    strata_update_texture_rgba8(engine(), tex, 0, 0, c.texW, c.texH, rgba);
}

// Buildings being tended right now (kBeaconStride floats each: x, y, z of
// the roof, r, g, b, strength).
STRATA_KIT_EXPORT void fc_set_beacons(const float* rows, int count) {
    c.beacons.assign(rows, rows + static_cast<size_t>(std::max(count, 0)) * kBeaconStride);
}

// Soft glows over lit roofs (kBeaconStride floats each: x, y, z, r, g, b,
// level).
STRATA_KIT_EXPORT void fc_set_glows(const float* rows, int count) {
    c.glows.assign(rows, rows + static_cast<size_t>(std::max(count, 0)) * kBeaconStride);
}

STRATA_KIT_EXPORT void fc_set_selected(int selected, int hovered) {
    c.selected = selected;
    c.hovered = hovered;
}

// The building under a point on the canvas, in CSS pixels from its top-left
// (cssW x cssH is the canvas's CSS size), or -1. Picking is done in CSS
// pixels against the last frame's camera (the kit's view-projection matrix
// and eye), so it does not depend on the device pixel ratio the canvas is
// drawn at.
STRATA_KIT_EXPORT int fc_pick(float cssX, float cssY, float cssW, float cssH) {
    const strata_kit::Host& h = host();
    if (!h.engine || cssW <= 0 || cssH <= 0) return -1;
    float inv[16];
    if (!invert4(h.viewProj, inv)) return -1;
    const float nx = 2.0f * cssX / cssW - 1.0f, ny = 1.0f - 2.0f * cssY / cssH;
    // A point at mid depth is on the ray under either depth convention.
    const float clip[4] = {nx, ny, 0.5f, 1.0f};
    float w[4] = {};
    for (int r = 0; r < 4; ++r)
        for (int k = 0; k < 4; ++k) w[r] += inv[k * 4 + r] * clip[k];
    if (std::fabs(w[3]) < 1e-9f) return -1;
    const float dir[3] = {w[0] / w[3] - h.eye[0], w[1] / w[3] - h.eye[1], w[2] / w[3] - h.eye[2]};
    StrataRayHit hit{};
    if (strata_raycast_meshes(h.engine, h.eye, dir, 1.0e6f, &hit) != STRATA_OK || hit.mesh_id == 0) return -1;
    for (size_t i = 0; i < c.meshIds.size(); ++i)
        if (c.meshIds[i] == hit.mesh_id) return tagOf(c.meshTags[i], static_cast<uint32_t>(hit.triangle));
    return -1;
}
