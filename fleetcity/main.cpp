// fleetcity: the repository fleet as a city, drawn by Strata (WebGL 2), for
// the hub's /city page (Stephenson-Software RFC 0026).
//
// The hub lays the city out (gardener/fleetcity.py) and the page
// (gardener/web/city.html) owns the data, input, labels and timelapse. This
// module only draws, and answers one question: which building is under a
// point on the canvas. It is handed numbers only: one row of kBuildingStride
// floats per building, one of kDistrictStride per district, and two RGBA8
// palettes with one texel per building.
//
// RFC 0026 §4: each district is one textured mesh for the bodies (lit) and
// one for the roofs (unlit, so a lit roof glows at night), never one mesh
// per repository (Strata holds at most 256). A per-triangle tag table maps a
// strata_raycast_meshes hit back to its building, the TriTag pattern of
// kindling's render bridge. Colours live in the two palette textures, so a
// timelapse frame is one strata_update_texture_rgba8 call and rebuilds no
// geometry.
//
// Build: fleetcity/build.sh (emsdk 6.0.11, Strata's wasm release, SHA-pinned).
#include <emscripten/emscripten.h>
#include <emscripten/html5.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <vector>

#include "strata/strata.h"

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
    StrataEngine* engine = nullptr;
    int width = 0, height = 0;
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
    float yaw = 0.8f, pitch = 0.75f, dist = 80.0f, target[3] = {0, 0, 0};
    float viewProj[16] = {};
    float eye[3] = {};
    float toSun[3] = {0.3f, 0.8f, 0.4f};
    float night = 0.0f;
    int quality = 2;
    bool reducedMotion = false;
    std::vector<StrataPoint> points;
    std::vector<StrataLineVertex> lines;
};

City c;

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
    if (strata_add_textured_mesh(c.engine, &tm, &id) != STRATA_OK) {
        std::fprintf(stderr, "fleetcity: strata_add_textured_mesh: %s\n", strata_last_error());
        return 0;
    }
    c.meshIds.push_back(id);
    c.meshTags.push_back(m.tags);
    return id;
}

void rebuild() {
    if (!c.engine) return;
    strata_clear_meshes(c.engine);
    c.meshIds.clear();
    c.meshTags.clear();
    if (c.bodyTexture) strata_destroy_texture(c.engine, c.bodyTexture);
    if (c.roofTexture) strata_destroy_texture(c.engine, c.roofTexture);
    c.bodyTexture = c.roofTexture = 0;
    std::vector<uint8_t> grey(static_cast<size_t>(c.texW) * c.texH * 4, 128);
    const uint32_t flags = STRATA_TEXTURE_NEAREST | STRATA_TEXTURE_CLAMP_U | STRATA_TEXTURE_CLAMP_V;
    strata_create_texture_rgba8(c.engine, c.texW, c.texH, grey.data(), flags, &c.bodyTexture);
    strata_create_texture_rgba8(c.engine, c.texW, c.texH, grey.data(), flags, &c.roofTexture);

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
    if (!c.engine) return;
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

void mul4(const float* a, const float* b, float* out) {  // column-major a * b
    for (int col = 0; col < 4; ++col)
        for (int r = 0; r < 4; ++r) {
            float s = 0.0f;
            for (int k = 0; k < 4; ++k) s += a[k * 4 + r] * b[col * 4 + k];
            out[col * 4 + r] = s;
        }
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
        c.lines.push_back({{ax, ay, az}, {r, g, bl, a}});
        c.lines.push_back({{bx, by, bz}, {r, g, bl, a}});
    };
    for (int i = 0; i < 4; ++i) {
        const int j = (i + 1) % 4;
        seg(px[i], y0, pz[i], px[j], y0, pz[j]);
        seg(px[i], y1, pz[i], px[j], y1, pz[j]);
        seg(px[i], y0, pz[i], px[i], y1, pz[i]);
    }
}

void* noProcAddress(const char*, void*) { return nullptr; }

}  // namespace

extern "C" {

// Creates the WebGL 2 context on the canvas and the engine in it. Returns 0
// on success, or a negative number (no WebGL 2) or a StrataStatus.
// preserve keeps the drawing buffer after compositing, for captures.
EMSCRIPTEN_KEEPALIVE int fc_init(const char* canvasSelector, int width, int height, int quality, int reducedMotion,
                                 int preserve) {
    EmscriptenWebGLContextAttributes attrs;
    emscripten_webgl_init_context_attributes(&attrs);
    attrs.majorVersion = 2;
    attrs.minorVersion = 0;
    attrs.alpha = false;
    attrs.depth = true;
    attrs.stencil = false;
    attrs.antialias = true;
    attrs.preserveDrawingBuffer = preserve != 0;
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
    const StrataStatus status = strata_create(&desc, &c.engine);
    if (status != STRATA_OK) {
        std::fprintf(stderr, "strata_create failed: %s (%s)\n", strata_status_string(status), strata_last_error());
        return status;
    }
    if (strata_abi_minor() < 10) {
        std::fprintf(stderr, "fleetcity: Strata ABI 1.%u is older than 1.10\n", strata_abi_minor());
        return STRATA_ERROR_UNSUPPORTED;
    }
    c.width = width;
    c.height = height;
    c.quality = quality;
    c.reducedMotion = reducedMotion != 0;
    std::printf("fleetcity: Strata %s on %s\n", strata_version_string(), strata_gpu_description(c.engine));
    return 0;
}

// 0: no shadows (software rendering, weak phones), 1: one small cascade,
// 2: the full three. The page steps down when frames run slow.
EMSCRIPTEN_KEEPALIVE void fc_set_quality(int quality) { c.quality = std::clamp(quality, 0, 2); }

EMSCRIPTEN_KEEPALIVE void fc_resize(int width, int height) {
    if (!c.engine || width <= 0 || height <= 0) return;
    c.width = width;
    c.height = height;
    strata_resize(c.engine, width, height);
}

// Replaces the whole city: buildings (kBuildingStride floats each), districts
// (kDistrictStride each), the palette size and the bounds. Both palettes are
// reset to grey; fc_set_palette fills them.
EMSCRIPTEN_KEEPALIVE int fc_set_city(const float* rows, int count, const float* districts, int districtCount,
                                     int texW, int texH, float x0, float z0, float x1, float z1) {
    if (texW < 1 || texH < 1 || texW > STRATA_MAX_TEXTURE_SIZE || texH > STRATA_MAX_TEXTURE_SIZE) return -1;
    c.texW = texW;
    c.texH = texH;
    c.bounds[0] = x0;
    c.bounds[1] = z0;
    c.bounds[2] = x1;
    c.bounds[3] = z1;
    c.districts.assign(districts, districts + static_cast<size_t>(districtCount) * kDistrictStride);
    c.buildings.clear();
    gDistrictOf.clear();
    for (int i = 0; i < count; ++i) {
        const float* r = rows + static_cast<size_t>(i) * kBuildingStride;
        const int id = static_cast<int>(r[0]);
        if (id < 0 || id >= texW * texH || !(r[6] > 0.0f) || !(r[4] > r[2]) || !(r[5] > r[3])) continue;
        c.buildings.push_back({id, r[2], r[3], r[4], r[5], r[6]});
        gDistrictOf.push_back(static_cast<int>(r[1]));
    }
    rebuildMeshes();
    return static_cast<int>(c.buildings.size());
}

// which 0: building bodies, 1: roofs. rgba is texW * texH * 4 bytes.
EMSCRIPTEN_KEEPALIVE void fc_set_palette(int which, const uint8_t* rgba) {
    const uint32_t tex = which == 0 ? c.bodyTexture : c.roofTexture;
    if (!c.engine || !tex) return;
    strata_update_texture_rgba8(c.engine, tex, 0, 0, c.texW, c.texH, rgba);
}

EMSCRIPTEN_KEEPALIVE void fc_set_beacons(const float* rows, int count) {
    c.beacons.assign(rows, rows + static_cast<size_t>(count) * kBeaconStride);
}

// Soft glows over lit roofs (kBeaconStride floats each: x, y, z, r, g, b,
// level), so a night's runs read at a distance where a roof is a pixel.
EMSCRIPTEN_KEEPALIVE void fc_set_glows(const float* rows, int count) {
    c.glows.assign(rows, rows + static_cast<size_t>(count) * kBeaconStride);
}

EMSCRIPTEN_KEEPALIVE void fc_set_selected(int selected, int hovered) {
    c.selected = selected;
    c.hovered = hovered;
}

EMSCRIPTEN_KEEPALIVE void fc_set_camera(float yaw, float pitch, float dist, float tx, float ty, float tz) {
    c.yaw = yaw;
    c.pitch = pitch;
    c.dist = dist;
    c.target[0] = tx;
    c.target[1] = ty;
    c.target[2] = tz;
}

// The sun as it really stands over (lat, lon) at unix_seconds. Returns its
// elevation in degrees.
EMSCRIPTEN_KEEPALIVE float fc_set_sun(double unixSeconds, double lat, double lon) {
    StrataSolarPosition sun{};
    if (strata_solar_position(unixSeconds, lat, lon, &sun) != STRATA_OK) return 0.0f;
    std::copy(sun.to_sun, sun.to_sun + 3, c.toSun);
    c.night = std::clamp((2.0f - sun.elevation_degrees) / 10.0f, 0.0f, 1.0f);
    if (c.engine) strata_set_sun_direction(c.engine, c.toSun);
    return sun.elevation_degrees;
}

// The view-projection matrix of the last frame (column-major), for the page
// to place labels with.
EMSCRIPTEN_KEEPALIVE const float* fc_view_proj() { return c.viewProj; }

// The building under a point on the canvas, in CSS pixels from its top-left
// (cssW x cssH is the canvas's CSS size), or -1. Picking is done in CSS
// pixels against the last frame's camera, so it does not depend on the
// device pixel ratio the canvas is drawn at.
EMSCRIPTEN_KEEPALIVE int fc_pick(float cssX, float cssY, float cssW, float cssH) {
    if (!c.engine || cssW <= 0 || cssH <= 0) return -1;
    float inv[16];
    if (!invert4(c.viewProj, inv)) return -1;
    const float nx = 2.0f * cssX / cssW - 1.0f, ny = 1.0f - 2.0f * cssY / cssH;
    // A point at mid depth is on the ray under either depth convention.
    const float clip[4] = {nx, ny, 0.5f, 1.0f};
    float w[4] = {};
    for (int r = 0; r < 4; ++r)
        for (int k = 0; k < 4; ++k) w[r] += inv[k * 4 + r] * clip[k];
    if (std::fabs(w[3]) < 1e-9f) return -1;
    const float dir[3] = {w[0] / w[3] - c.eye[0], w[1] / w[3] - c.eye[1], w[2] / w[3] - c.eye[2]};
    StrataRayHit hit{};
    if (strata_raycast_meshes(c.engine, c.eye, dir, 1.0e6f, &hit) != STRATA_OK || hit.mesh_id == 0) return -1;
    for (size_t i = 0; i < c.meshIds.size(); ++i)
        if (c.meshIds[i] == hit.mesh_id) return tagOf(c.meshTags[i], static_cast<uint32_t>(hit.triangle));
    return -1;
}

EMSCRIPTEN_KEEPALIVE int fc_frame(double seconds) {
    if (!c.engine || c.width <= 0 || c.height <= 0) return 0;

    const float cp = std::cos(c.pitch);
    c.eye[0] = c.target[0] + c.dist * cp * std::cos(c.yaw);
    c.eye[1] = c.target[1] + c.dist * std::sin(c.pitch);
    c.eye[2] = c.target[2] + c.dist * cp * std::sin(c.yaw);
    const float up[3] = {0.0f, 1.0f, 0.0f};
    StrataCamera camera{};
    strata_camera_look_at(c.eye, c.target, up, 45.0f, static_cast<float>(c.width) / c.height, 0.5f,
                          std::max(600.0f, c.dist * 4.0f), &camera);
    strata_set_camera(c.engine, &camera);
    mul4(camera.projection, camera.view, c.viewProj);

    StrataLighting lighting{};
    strata_lighting_defaults(STRATA_LIGHTING_SUN, &lighting);
    lighting.cascade_count = c.quality == 2 ? 3 : c.quality;
    lighting.shadow_map_size = c.quality == 2 ? 2048 : 1024;
    lighting.shadow_distance = std::max(60.0f, c.dist * 2.0f);
    lighting.fog = 1;
    strata_set_lighting(c.engine, &lighting);

    c.points.clear();
    c.lines.clear();
    std::vector<StrataPointLight> lights;
    // Buildings being tended right now: a beacon over the roof, pulsing.
    for (size_t i = 0; i + kBeaconStride <= c.beacons.size(); i += kBeaconStride) {
        const float* b = &c.beacons[i];
        const float pulse = c.reducedMotion ? 1.0f : 0.75f + 0.25f * static_cast<float>(std::sin(seconds * 3.0 + i));
        StrataPoint p{};
        p.position[0] = b[0];
        p.position[1] = b[1] + 0.9f;
        p.position[2] = b[2];
        p.size_pixels = 14.0f * pulse;
        p.world_diameter = 1.4f;
        p.color[0] = b[3];
        p.color[1] = b[4];
        p.color[2] = b[5];
        p.color[3] = std::clamp(b[6], 0.0f, 1.0f);
        p.flags = STRATA_POINT_SOFT;
        c.points.push_back(p);
        for (int k = 0; k < 6; ++k) {  // a column of light down to the roof
            StrataPoint q = p;
            q.position[1] = b[1] + 0.15f * k;
            q.size_pixels = 5.0f;
            q.world_diameter = 0.35f;
            q.color[3] *= 0.6f;
            c.points.push_back(q);
        }
        if (lights.size() < 8)
            lights.push_back({{b[0], b[1] + 1.0f, b[2]}, 7.0f, {b[3], b[4], b[5]}, (1.0f + 2.0f * c.night) * pulse, 0u});
    }
    for (size_t i = 0; i + kBeaconStride <= c.glows.size(); i += kBeaconStride) {
        const float* g = &c.glows[i];
        const float level = std::clamp(g[6], 0.0f, 1.0f);
        StrataPoint p{};
        p.position[0] = g[0];
        p.position[1] = g[1] + 0.25f;
        p.position[2] = g[2];
        p.size_pixels = 4.0f + 8.0f * level;
        p.world_diameter = 1.4f;
        p.color[0] = g[3];
        p.color[1] = g[4];
        p.color[2] = g[5];
        p.color[3] = 0.25f + 0.6f * level;
        p.flags = STRATA_POINT_SOFT;
        c.points.push_back(p);
    }
    // Street lamps at the district corners, lit after dusk.
    if (c.night > 0.02f) {
        for (size_t i = 0; i + kDistrictStride <= c.districts.size() && lights.size() + 4 <= STRATA_MAX_POINT_LIGHTS;
             i += kDistrictStride) {
            const float* d = &c.districts[i];
            const float xs[2] = {d[0] - 0.6f, d[2] + 0.6f}, zs[2] = {d[1] - 0.6f, d[3] + 0.6f};
            const float reach = std::max(d[2] - d[0], d[3] - d[1]) * 0.6f;
            for (float x : xs)
                for (float z : zs) {
                    lights.push_back({{x, 3.0f, z}, reach, {1.0f, 0.78f, 0.5f}, 0.9f * c.night, 0u});
                    StrataPoint p{};
                    p.position[0] = x;
                    p.position[1] = 3.0f;
                    p.position[2] = z;
                    p.size_pixels = 4.0f;
                    p.world_diameter = 0.5f;
                    p.color[0] = 1.0f;
                    p.color[1] = 0.8f;
                    p.color[2] = 0.5f;
                    p.color[3] = c.night;
                    p.flags = STRATA_POINT_SOFT;
                    c.points.push_back(p);
                }
        }
    }
    strata_set_point_lights(c.engine, lights.data(), static_cast<int32_t>(lights.size()));
    if (const Building* b = findBuilding(c.hovered)) outline(*b, 0.85f, 0.9f, 1.0f, 0.55f);
    if (const Building* b = findBuilding(c.selected)) outline(*b, 1.0f, 1.0f, 1.0f, 0.95f);

    StrataFrameDesc desc{};
    desc.clear_flags = STRATA_CLEAR_COLOR | STRATA_CLEAR_DEPTH | STRATA_CLEAR_SKY;
    desc.clear_color[3] = 1.0f;
    if (strata_begin_frame(c.engine, &desc) != STRATA_OK) {
        std::fprintf(stderr, "strata_begin_frame: %s\n", strata_last_error());
        return 0;
    }
    if (!c.lines.empty()) strata_submit_lines(c.engine, c.lines.data(), static_cast<int32_t>(c.lines.size()));
    if (!c.points.empty()) strata_submit_points(c.engine, c.points.data(), static_cast<int32_t>(c.points.size()));
    if (strata_end_frame(c.engine) != STRATA_OK) {
        std::fprintf(stderr, "strata_end_frame: %s\n", strata_last_error());
        return 0;
    }
    strata_present(c.engine);
    return static_cast<int>(c.buildings.size());
}

}  // extern "C"
