// strata_kit.h: the host shell for a Strata scene embedded in a web page.
//
// Part of the Strata web kit (docs/WEB.md, "The web kit"). It covers the shell
// around a scene and leaves the scene alone: the WebGL 2 context, the engine
// in it, resizing, three quality tiers, an orbit camera, the sun, the per-frame
// batching of primitives, points, lines and point lights, and a stats export.
// The scene is the host's: it fills the batches in a hook, from whatever data
// its own exported setters were handed.
//
// Use: in exactly one translation unit,
//
//     #define STRATA_KIT_MAIN
//     #include "strata_kit.h"
//
//     void strata_kit_configure(strata_kit::Options& o) { o.name = "garden"; }
//     void strata_kit_engine_ready(strata_kit::Host& h) { /* upload terrain, meshes */ }
//     void strata_kit_build(strata_kit::Host& h, double t) { h.add(...); h.light(...); }
//
// and link with the `sk_*` exports below (build-kit.sh adds them). The three
// hooks are required. `strata_kit_engine_ready` runs after the engine is made
// and again after a lost WebGL context is restored (`sk_restore`), so anything
// uploaded once (terrain, meshes, textures) belongs there.
//
// The same header builds natively (no Emscripten): `sk_init_headless` opens a
// headless engine and `sk_save_png` writes the last frame, so a scene that is
// "state in, frame out" also renders PNGs for previews and goldens.
//
// Picking and labels are the page's (strata-kit.js), in CSS pixels, from the
// matrix `sk_view_proj` exports. The engine's drawing buffer is never used for
// pointer maths, so the device pixel ratio never enters picking.
//
// MIT licensed, as Strata.
#ifndef STRATA_KIT_H
#define STRATA_KIT_H

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <vector>

#include "strata/strata.h"

#ifdef __EMSCRIPTEN__
#include <emscripten/emscripten.h>
#include <emscripten/html5.h>
#define STRATA_KIT_EXPORT extern "C" EMSCRIPTEN_KEEPALIVE
#else
#define STRATA_KIT_EXPORT extern "C"
#endif

// The kit's own version, separate from the engine's: a page and its wasm are
// built from one kit, and strata-kit.js checks they agree (sk_kit_version).
#define STRATA_KIT_VERSION_MAJOR 1
#define STRATA_KIT_VERSION_MINOR 0
#define STRATA_KIT_VERSION_STRING "1.0"

namespace strata_kit {

struct Rgb {
    float r, g, b;
};

// One quality tier. The page starts at tier 2 (1 on a coarse pointer), and
// strata-kit.js steps down when frames run slow, after it has first lowered
// the pixel density.
struct Tier {
    int cascades;       // sun shadow cascades: 0 turns shadows off
    int shadowMapSize;  // per cascade
    int maxLights;      // point lights kept, highest priority first (<= STRATA_MAX_POINT_LIGHTS)
    bool thinPoints;    // draw only every other soft point (smoke, sparks, dust thin out)
};

struct Options {
    const char* name = "strata-kit";  // printed once at start
    bool preserveDrawingBuffer = true;  // so a page or a tool can read frames back
    bool antialias = true;
    float fovDegrees = 50.0f;
    float nearPlane = 0.3f;
    float farPlane = 400.0f;
    float shadowDistanceMin = 40.0f;      // shadow distance is max(min, dist * perDist)
    float shadowDistancePerDist = 2.2f;
    bool fog = true;
    // 0: no shadows (software rendering, weak phones), 1: one cascade, 2: three.
    Tier tiers[3] = {{0, 1024, 4, false}, {1, 1024, 8, false}, {3, 2048, 16, false}};
};

struct Light {
    StrataPointLight light;
    int priority;  // higher is kept first when there are more than the tier allows
};

struct Host {
    Options opts;
    StrataEngine* engine = nullptr;
    int width = 0, height = 0;
    int quality = 2;
    bool reducedMotion = false;
    bool headless = false;
    // Orbit camera: yaw and pitch in radians around target, at dist.
    float yaw = 0.8f, pitch = 0.6f, dist = 30.0f, target[3] = {0.0f, 0.0f, 0.0f};
    float eye[3] = {0.0f, 0.0f, 0.0f};
    float viewProj[16] = {};  // of the last frame, column-major
    // Sun: direction to it, and 0 by day .. 1 at night (from its elevation).
    float toSun[3] = {0.3f, 0.8f, 0.4f};
    float night = 0.0f;
    float sunElevation = 45.0f;
    // This frame's batches. The kit clears them before strata_kit_build.
    std::vector<StrataRenderItem> items;
    std::vector<StrataPoint> points;
    std::vector<StrataLineVertex> lines;
    std::vector<Light> lights;
    // Stats of the last frame (sk_stats).
    uint32_t pointSeq = 0;
    int32_t lightsDrawn = 0;
    int32_t terrainSamples = 0;  // the host sets this when it uploads terrain
    int32_t frames = 0;
    int32_t pointsRejected = 0;
#ifdef __EMSCRIPTEN__
    EMSCRIPTEN_WEBGL_CONTEXT_HANDLE context = 0;
#endif
    char selector[128] = {};

    const Tier& tier() const { return opts.tiers[std::clamp(quality, 0, 2)]; }

    void add(int kind, float x, float y, float z, float sx, float sy, float sz, Rgb c, float a = 1.0f) {
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
        it.color[3] = a;
        items.push_back(it);
    }

    // A point sprite. Alpha is clamped: Strata rejects the whole call over one
    // alpha outside 0..1. A tier with thinPoints keeps every other soft point.
    void point(float x, float y, float z, float sizePixels, float worldDiameter, Rgb c, float a,
               uint32_t flags = STRATA_POINT_SOFT) {
        if (tier().thinPoints && (flags & STRATA_POINT_SOFT) && (pointSeq++ & 1u)) return;
        StrataPoint p{};
        p.position[0] = x;
        p.position[1] = y;
        p.position[2] = z;
        p.size_pixels = sizePixels;
        p.world_diameter = worldDiameter;
        p.color[0] = c.r;
        p.color[1] = c.g;
        p.color[2] = c.b;
        p.color[3] = std::clamp(a, 0.0f, 1.0f);
        p.flags = flags;
        points.push_back(p);
    }

    void line(float x0, float y0, float z0, float x1, float y1, float z1, Rgb c, float a = 1.0f) {
        const float alpha = std::clamp(a, 0.0f, 1.0f);
        StrataLineVertex v0{{x0, y0, z0}, {c.r, c.g, c.b, alpha}};
        StrataLineVertex v1{{x1, y1, z1}, {c.r, c.g, c.b, alpha}};
        lines.push_back(v0);
        lines.push_back(v1);
    }

    // A point light. All of a frame's lights are sorted by priority (stably,
    // so equal priorities keep their order) and cut to the tier's budget.
    void light(const StrataPointLight& l, int priority = 0) { lights.push_back({l, priority}); }
    void light(float x, float y, float z, float range, Rgb c, float intensity, int priority = 0) {
        StrataPointLight l{};
        l.position[0] = x;
        l.position[1] = y;
        l.position[2] = z;
        l.range = range;
        l.color[0] = c.r;
        l.color[1] = c.g;
        l.color[2] = c.b;
        l.intensity = intensity;
        light(l, priority);
    }
};

// The one host of this module.
inline Host& host() {
    static Host h;
    return h;
}

inline void mul4(const float* a, const float* b, float* out) {  // column-major a * b
    for (int c = 0; c < 4; ++c)
        for (int r = 0; r < 4; ++r) {
            float s = 0.0f;
            for (int k = 0; k < 4; ++k) s += a[k * 4 + r] * b[c * 4 + k];
            out[c * 4 + r] = s;
        }
}

inline void* noProcAddress(const char*, void*) { return nullptr; }

}  // namespace strata_kit

// The host's hooks (see the top of this file).
void strata_kit_configure(strata_kit::Options& opts);
void strata_kit_engine_ready(strata_kit::Host& host);
void strata_kit_build(strata_kit::Host& host, double seconds);

#ifdef STRATA_KIT_MAIN

namespace strata_kit {
namespace detail {

inline int createEngine(Host& h, int surface) {
    StrataEngineDesc desc{};
    desc.struct_size = sizeof desc;
    desc.abi_version = STRATA_ABI_VERSION;
    desc.surface = surface;
    desc.width = h.width;
    desc.height = h.height;
    if (surface == STRATA_SURFACE_HOST_CONTEXT) desc.get_proc_address = noProcAddress;  // never called in a browser
    const StrataStatus status = strata_create(&desc, &h.engine);
    if (status != STRATA_OK) {
        std::fprintf(stderr, "%s: strata_create failed: %s (%s)\n", h.opts.name, strata_status_string(status),
                     strata_last_error());
        h.engine = nullptr;
        return status;
    }
    strata_set_sun_direction(h.engine, h.toSun);
    return 0;
}

#ifdef __EMSCRIPTEN__
inline int createContext(Host& h) {
    EmscriptenWebGLContextAttributes attrs;
    emscripten_webgl_init_context_attributes(&attrs);
    attrs.majorVersion = 2;
    attrs.minorVersion = 0;
    attrs.alpha = false;
    attrs.depth = true;
    attrs.stencil = false;
    attrs.antialias = h.opts.antialias;
    attrs.preserveDrawingBuffer = h.opts.preserveDrawingBuffer;
    h.context = emscripten_webgl_create_context(h.selector, &attrs);
    if (h.context <= 0) {
        h.context = 0;
        return -1;
    }
    emscripten_webgl_make_context_current(h.context);
    return 0;
}
#endif

inline bool configured = false;

inline void configureOnce(Host& h) {
    if (configured) return;
    configured = true;
    strata_kit_configure(h.opts);
    for (Tier& t : h.opts.tiers) {
        t.cascades = std::clamp(t.cascades, 0, 4);
        t.maxLights = std::clamp(t.maxLights, 0, static_cast<int>(STRATA_MAX_POINT_LIGHTS));
    }
}

}  // namespace detail
}  // namespace strata_kit

#ifdef __EMSCRIPTEN__
// Creates the WebGL 2 context on the canvas (a CSS selector, e.g. "#view") and
// the engine in it. Returns 0, -1 when there is no WebGL 2, or a StrataStatus.
STRATA_KIT_EXPORT int sk_init(const char* canvasSelector, int width, int height, int quality, int reducedMotion) {
    using namespace strata_kit;
    Host& h = host();
    detail::configureOnce(h);
    std::snprintf(h.selector, sizeof h.selector, "%s", canvasSelector ? canvasSelector : "#canvas");
    h.width = std::max(width, 1);
    h.height = std::max(height, 1);
    h.quality = std::clamp(quality, 0, 2);
    h.reducedMotion = reducedMotion != 0;
    if (detail::createContext(h) != 0) return -1;
    const int rc = detail::createEngine(h, STRATA_SURFACE_HOST_CONTEXT);
    if (rc != 0) return rc;
    std::printf("%s: Strata %s on %s (kit %s)\n", h.opts.name, strata_version_string(),
                strata_gpu_description(h.engine), STRATA_KIT_VERSION_STRING);
    strata_kit_engine_ready(h);
    return 0;
}

// After the page's canvas fired `webglcontextrestored`: the old engine's GL
// objects died with the lost context, so the engine is destroyed (its deletes
// go to the lost context and do nothing), a new context handle is made on the
// same canvas, and a new engine in it. strata_kit_engine_ready runs again.
// Returns as sk_init does.
STRATA_KIT_EXPORT int sk_restore() {
    using namespace strata_kit;
    Host& h = host();
    if (h.engine) {
        if (h.context) emscripten_webgl_make_context_current(h.context);
        strata_destroy(h.engine);
        h.engine = nullptr;
    }
    if (h.context) {
        emscripten_webgl_make_context_current(0);
        emscripten_webgl_destroy_context(h.context);
        h.context = 0;
    }
    if (detail::createContext(h) != 0) return -1;
    const int rc = detail::createEngine(h, STRATA_SURFACE_HOST_CONTEXT);
    if (rc != 0) return rc;
    std::printf("%s: engine restored after a lost WebGL context\n", h.opts.name);
    strata_kit_engine_ready(h);
    return 0;
}
#else
// Natively: a headless engine (EGL surfaceless) of width x height.
STRATA_KIT_EXPORT int sk_init_headless(int width, int height, int quality) {
    using namespace strata_kit;
    Host& h = host();
    detail::configureOnce(h);
    h.width = std::max(width, 1);
    h.height = std::max(height, 1);
    h.quality = std::clamp(quality, 0, 2);
    h.reducedMotion = true;
    h.headless = true;
    const int rc = detail::createEngine(h, STRATA_SURFACE_HEADLESS);
    if (rc != 0) return rc;
    strata_kit_engine_ready(h);
    return 0;
}

// Writes the last frame as a PNG.
STRATA_KIT_EXPORT int sk_save_png(const char* path) {
    using namespace strata_kit;
    if (!host().engine) return STRATA_ERROR_INVALID_STATE;
    return strata_save_screenshot_png(host().engine, path);
}

STRATA_KIT_EXPORT void sk_shutdown() {
    using namespace strata_kit;
    if (host().engine) strata_destroy(host().engine);
    host().engine = nullptr;
}
#endif

STRATA_KIT_EXPORT const char* sk_kit_version() { return STRATA_KIT_VERSION_STRING; }

// 0, 1 or 2: see Options::tiers.
STRATA_KIT_EXPORT void sk_set_quality(int quality) { strata_kit::host().quality = std::clamp(quality, 0, 2); }

// The drawing buffer's size in device pixels.
STRATA_KIT_EXPORT void sk_resize(int width, int height) {
    strata_kit::Host& h = strata_kit::host();
    if (!h.engine || width <= 0 || height <= 0) return;
    h.width = width;
    h.height = height;
    strata_resize(h.engine, width, height);
}

STRATA_KIT_EXPORT void sk_set_camera(float yaw, float pitch, float dist, float tx, float ty, float tz) {
    strata_kit::Host& h = strata_kit::host();
    h.yaw = yaw;
    h.pitch = pitch;
    h.dist = dist;
    h.target[0] = tx;
    h.target[1] = ty;
    h.target[2] = tz;
}

// The sun as it really stands over (lat, lon) at unix_seconds. Returns its
// elevation in degrees; Host::night follows it (0 above 2 degrees, 1 below -8).
STRATA_KIT_EXPORT float sk_set_sun(double unixSeconds, double lat, double lon) {
    strata_kit::Host& h = strata_kit::host();
    StrataSolarPosition sun{};
    if (strata_solar_position(unixSeconds, lat, lon, &sun) != STRATA_OK) return 0.0f;
    std::copy(sun.to_sun, sun.to_sun + 3, h.toSun);
    h.sunElevation = sun.elevation_degrees;
    h.night = std::clamp((2.0f - sun.elevation_degrees) / 10.0f, 0.0f, 1.0f);
    if (h.engine) strata_set_sun_direction(h.engine, h.toSun);
    return sun.elevation_degrees;
}

// The view-projection matrix of the last frame (column-major, 16 floats).
STRATA_KIT_EXPORT const float* sk_view_proj() { return strata_kit::host().viewProj; }

// What the last frame drew: primitives, points, line segments, point lights,
// terrain samples, quality, width, height, frames drawn, points rejected.
STRATA_KIT_EXPORT const int32_t* sk_stats() {
    strata_kit::Host& h = strata_kit::host();
    static int32_t s[10];
    s[0] = static_cast<int32_t>(h.items.size());
    s[1] = static_cast<int32_t>(h.points.size());
    s[2] = static_cast<int32_t>(h.lines.size() / 2);
    s[3] = h.lightsDrawn;
    s[4] = h.terrainSamples;
    s[5] = h.quality;
    s[6] = h.width;
    s[7] = h.height;
    s[8] = h.frames;
    s[9] = h.pointsRejected;
    return s;
}

// Draws one frame at scene time `seconds`: the camera and lighting from the
// tier, then strata_kit_build, then the batches. Returns the primitive count.
STRATA_KIT_EXPORT int sk_frame(double seconds) {
    using namespace strata_kit;
    Host& h = host();
    if (!h.engine || h.width <= 0 || h.height <= 0) return 0;
    const Options& o = h.opts;
    const Tier& tier = h.tier();

    const float cp = std::cos(h.pitch);
    h.eye[0] = h.target[0] + h.dist * cp * std::cos(h.yaw);
    h.eye[1] = h.target[1] + h.dist * std::sin(h.pitch);
    h.eye[2] = h.target[2] + h.dist * cp * std::sin(h.yaw);
    const float up[3] = {0.0f, 1.0f, 0.0f};
    StrataCamera camera{};
    strata_camera_look_at(h.eye, h.target, up, o.fovDegrees, static_cast<float>(h.width) / h.height, o.nearPlane,
                          o.farPlane, &camera);
    strata_set_camera(h.engine, &camera);
    mul4(camera.projection, camera.view, h.viewProj);

    StrataLighting lighting{};
    strata_lighting_defaults(STRATA_LIGHTING_SUN, &lighting);
    lighting.cascade_count = tier.cascades;
    lighting.shadow_map_size = tier.shadowMapSize;
    lighting.shadow_distance = std::max(o.shadowDistanceMin, h.dist * o.shadowDistancePerDist);
    lighting.fog = o.fog ? 1 : 0;
    strata_set_lighting(h.engine, &lighting);

    h.items.clear();
    h.points.clear();
    h.lines.clear();
    h.lights.clear();
    h.pointSeq = 0;
    strata_kit_build(h, seconds);

    std::stable_sort(h.lights.begin(), h.lights.end(),
                     [](const Light& a, const Light& b) { return a.priority > b.priority; });
    StrataPointLight kept[STRATA_MAX_POINT_LIGHTS];
    int32_t n = 0;
    for (const Light& l : h.lights) {
        if (n >= tier.maxLights) break;
        kept[n++] = l.light;
    }
    h.lightsDrawn = n;
    strata_set_point_lights(h.engine, kept, n);

    StrataFrameDesc desc{};
    desc.clear_flags = STRATA_CLEAR_COLOR | STRATA_CLEAR_DEPTH | STRATA_CLEAR_SKY;
    desc.clear_color[3] = 1.0f;
    if (strata_begin_frame(h.engine, &desc) != STRATA_OK) {
        std::fprintf(stderr, "%s: strata_begin_frame: %s\n", o.name, strata_last_error());
        return 0;
    }
    if (!h.items.empty()) strata_submit_primitives(h.engine, h.items.data(), static_cast<int32_t>(h.items.size()));
    if (!h.lines.empty()) strata_submit_lines(h.engine, h.lines.data(), static_cast<int32_t>(h.lines.size()));
    if (!h.points.empty() &&
        strata_submit_points(h.engine, h.points.data(), static_cast<int32_t>(h.points.size())) != STRATA_OK) {
        // One bad point drops the whole call; say so once, not every frame.
        if (h.pointsRejected++ == 0) std::fprintf(stderr, "%s: strata_submit_points: %s\n", o.name, strata_last_error());
    }
    if (strata_end_frame(h.engine) != STRATA_OK) {
        std::fprintf(stderr, "%s: strata_end_frame: %s\n", o.name, strata_last_error());
        return 0;
    }
    strata_present(h.engine);
    ++h.frames;
    return static_cast<int>(h.items.size());
}

#endif  // STRATA_KIT_MAIN
#endif  // STRATA_KIT_H
