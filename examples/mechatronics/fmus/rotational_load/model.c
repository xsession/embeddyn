/*
 * Rotational load as an FMI co-simulation FMU (Embeddyn example).
 *   J dw/dt = tau - b w - coulomb tanh(w / 1e-3) - tau_load,   dtheta/dt = w
 * Integrated with fixed-step RK4 (h <= 10 us) inside each doStep.
 * Built twice by build_fmu.py: as FMI 2.0 (default) and with -DFMI3 as FMI 3.0.
 * Only the functions a co-simulation master needs for real variables are
 * implemented, plus FMU state get/set/serialize (the state is the whole Model
 * struct), so masters can checkpoint and roll back; others are omitted.
 */
#include <math.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#define TOKEN "{8c4e810f-3df3-4a00-8276-176fa3c9f000}"
enum { VR_TAU, VR_J, VR_B, VR_TAU_LOAD, VR_COULOMB, VR_W, VR_THETA, VR_ACCEL, VR_COUNT };
enum { OK = 0, ERROR = 3 };

typedef struct {
    double v[VR_COUNT];
    double time;
} Model;

static double accel(const Model *m, double w)
{
    return (m->v[VR_TAU] - m->v[VR_B] * w - m->v[VR_COULOMB] * tanh(w / 1e-3) - m->v[VR_TAU_LOAD]) / m->v[VR_J];
}

static void *create(const char *token)
{
    if (!token || strcmp(token, TOKEN) != 0) {
        return NULL;
    }
    Model *m = calloc(1, sizeof(Model));
    if (m) {
        m->v[VR_J] = 0.02;
        m->v[VR_B] = 0.01;
    }
    return m;
}

static int set(void *c, const unsigned *vr, size_t n, const double *v)
{
    Model *m = c;
    for (size_t i = 0; i < n; i++) {
        if (vr[i] >= VR_COUNT || (vr[i] == VR_J && v[i] <= 0)) {
            return ERROR;
        }
        m->v[vr[i]] = v[i];
    }
    return OK;
}

static int get(void *c, const unsigned *vr, size_t n, double *v)
{
    Model *m = c;
    m->v[VR_ACCEL] = accel(m, m->v[VR_W]);
    for (size_t i = 0; i < n; i++) {
        if (vr[i] >= VR_COUNT) {
            return ERROR;
        }
        v[i] = m->v[vr[i]];
    }
    return OK;
}

static int step(void *c, double t, double h)
{
    Model *m = c;
    int n = (int)ceil(h / 1e-5 - 1e-9);
    if (n < 1) {
        n = 1;
    }
    double dt = h / n, w = m->v[VR_W], th = m->v[VR_THETA];
    for (int i = 0; i < n; i++) {
        double k1 = accel(m, w), k2 = accel(m, w + dt / 2 * k1), k3 = accel(m, w + dt / 2 * k2), k4 = accel(m, w + dt * k3);
        th += dt / 6 * (w + 2 * (w + dt / 2 * k1) + 2 * (w + dt / 2 * k2) + (w + dt * k3));
        w += dt / 6 * (k1 + 2 * k2 + 2 * k3 + k4);
    }
    m->v[VR_W] = w;
    m->v[VR_THETA] = th;
    m->time = t + h;
    return isfinite(w) ? OK : ERROR;
}

/* FMU state = a copy of the Model struct (shared by FMI 2 and FMI 3). */
static int state_get(void *c, void **state)
{
    if (*state == NULL && (*state = malloc(sizeof(Model))) == NULL) return ERROR;
    memcpy(*state, c, sizeof(Model));
    return OK;
}
static int state_set(void *c, void *state) { if (!state) return ERROR; memcpy(c, state, sizeof(Model)); return OK; }
static int state_free(void **state) { free(*state); *state = NULL; return OK; }
static int state_size(size_t *size) { *size = sizeof(Model); return OK; }
static int state_serialize(void *state, unsigned char *buffer, size_t size)
{
    if (size < sizeof(Model)) return ERROR;
    memcpy(buffer, state, sizeof(Model));
    return OK;
}
static int state_deserialize(const unsigned char *buffer, size_t size, void **state)
{
    if (size != sizeof(Model)) return ERROR;
    if (*state == NULL && (*state = malloc(sizeof(Model))) == NULL) return ERROR;
    memcpy(*state, buffer, sizeof(Model));
    return OK;
}

#ifndef FMI3
/* ---- FMI 2.0 ---- */
const char *fmi2GetTypesPlatform(void) { return "default"; }
const char *fmi2GetVersion(void) { return "2.0"; }
void *fmi2Instantiate(const char *name, int type, const char *guid, const char *resources, const void *functions,
                      int visible, int logging)
{
    (void)name; (void)resources; (void)functions; (void)visible; (void)logging;
    return type == 1 ? create(guid) : NULL; /* 1 = fmi2CoSimulation */
}
int fmi2SetupExperiment(void *c, int tolDefined, double tol, double start, int stopDefined, double stop)
{
    (void)tolDefined; (void)tol; (void)stopDefined; (void)stop;
    ((Model *)c)->time = start;
    return OK;
}
int fmi2EnterInitializationMode(void *c) { (void)c; return OK; }
int fmi2ExitInitializationMode(void *c) { (void)c; return OK; }
int fmi2SetReal(void *c, const unsigned *vr, size_t n, const double *v) { return set(c, vr, n, v); }
int fmi2GetReal(void *c, const unsigned *vr, size_t n, double *v) { return get(c, vr, n, v); }
int fmi2DoStep(void *c, double t, double h, int noSetPrior) { (void)noSetPrior; return step(c, t, h); }
int fmi2Terminate(void *c) { (void)c; return OK; }
void fmi2FreeInstance(void *c) { free(c); }
int fmi2GetFMUstate(void *c, void **s) { return state_get(c, s); }
int fmi2SetFMUstate(void *c, void *s) { return state_set(c, s); }
int fmi2FreeFMUstate(void *c, void **s) { (void)c; return state_free(s); }
int fmi2SerializedFMUstateSize(void *c, void *s, size_t *n) { (void)c; (void)s; return state_size(n); }
int fmi2SerializeFMUstate(void *c, void *s, unsigned char *b, size_t n) { (void)c; return state_serialize(s, b, n); }
int fmi2DeSerializeFMUstate(void *c, const unsigned char *b, size_t n, void **s) { (void)c; return state_deserialize(b, n, s); }
/* The rest of the FMI 2.0 API, so generic importers (FMPy) can bind every symbol.
 * Unsupported here: they return an error. Declared without a prototype on purpose:
 * they ignore their arguments (callers clean up the stack on every supported ABI). */
int fmi2CancelStep() { return ERROR; }
int fmi2CompletedIntegratorStep() { return ERROR; }
int fmi2EnterContinuousTimeMode() { return ERROR; }
int fmi2EnterEventMode() { return ERROR; }
int fmi2GetBoolean() { return ERROR; }
int fmi2GetBooleanStatus() { return ERROR; }
int fmi2GetContinuousStates() { return ERROR; }
int fmi2GetDerivatives() { return ERROR; }
int fmi2GetDirectionalDerivative() { return ERROR; }
int fmi2GetEventIndicators() { return ERROR; }
int fmi2GetInteger() { return ERROR; }
int fmi2GetIntegerStatus() { return ERROR; }
int fmi2GetNominalsOfContinuousStates() { return ERROR; }
int fmi2GetRealOutputDerivatives() { return ERROR; }
int fmi2GetRealStatus() { return ERROR; }
int fmi2GetStatus() { return ERROR; }
int fmi2GetString() { return ERROR; }
int fmi2GetStringStatus() { return ERROR; }
int fmi2NewDiscreteStates() { return ERROR; }
int fmi2Reset() { return ERROR; }
int fmi2SetBoolean() { return ERROR; }
int fmi2SetContinuousStates() { return ERROR; }
int fmi2SetInteger() { return ERROR; }
int fmi2SetRealInputDerivatives() { return ERROR; }
int fmi2SetString() { return ERROR; }
int fmi2SetTime() { return ERROR; }
int fmi2SetDebugLogging() { return OK; }
#else
/* ---- FMI 3.0 ---- */
const char *fmi3GetVersion(void) { return "3.0"; }
void *fmi3InstantiateCoSimulation(const char *name, const char *token, const char *resources, bool visible,
                                  bool logging, bool eventMode, bool earlyReturn, const unsigned *required,
                                  size_t nRequired, void *env, void *logger, void *intermediate)
{
    (void)name; (void)resources; (void)visible; (void)logging; (void)eventMode; (void)earlyReturn;
    (void)required; (void)nRequired; (void)env; (void)logger; (void)intermediate;
    return create(token);
}
int fmi3EnterInitializationMode(void *c, bool tolDefined, double tol, double start, bool stopDefined, double stop)
{
    (void)tolDefined; (void)tol; (void)stopDefined; (void)stop;
    ((Model *)c)->time = start;
    return OK;
}
int fmi3ExitInitializationMode(void *c) { (void)c; return OK; }
int fmi3SetFloat64(void *c, const unsigned *vr, size_t n, const double *v, size_t nv) { (void)nv; return set(c, vr, n, v); }
int fmi3GetFloat64(void *c, const unsigned *vr, size_t n, double *v, size_t nv) { (void)nv; return get(c, vr, n, v); }
int fmi3DoStep(void *c, double t, double h, bool noSetPrior, bool *eventNeeded, bool *terminate, bool *early,
               double *lastTime)
{
    (void)noSetPrior;
    *eventNeeded = false;
    *terminate = false;
    *early = false;
    int status = step(c, t, h);
    *lastTime = ((Model *)c)->time;
    return status;
}
int fmi3Terminate(void *c) { (void)c; return OK; }
void fmi3FreeInstance(void *c) { free(c); }
int fmi3GetFMUState(void *c, void **s) { return state_get(c, s); }
int fmi3SetFMUState(void *c, void *s) { return state_set(c, s); }
int fmi3FreeFMUState(void *c, void **s) { (void)c; return state_free(s); }
int fmi3SerializedFMUStateSize(void *c, void *s, size_t *n) { (void)c; (void)s; return state_size(n); }
int fmi3SerializeFMUState(void *c, void *s, unsigned char *b, size_t n) { (void)c; return state_serialize(s, b, n); }
int fmi3DeserializeFMUState(void *c, const unsigned char *b, size_t n, void **s) { (void)c; return state_deserialize(b, n, s); }
/* The rest of the FMI 3.0 API, so generic importers (FMPy) can bind every symbol.
 * Unsupported here: they return an error. Declared without a prototype on purpose:
 * they ignore their arguments (callers clean up the stack on every supported ABI). */
int fmi3ActivateModelPartition() { return ERROR; }
int fmi3CompletedIntegratorStep() { return ERROR; }
int fmi3EnterConfigurationMode() { return ERROR; }
int fmi3EnterContinuousTimeMode() { return ERROR; }
int fmi3EnterEventMode() { return ERROR; }
int fmi3EnterStepMode() { return ERROR; }
int fmi3EvaluateDiscreteStates() { return ERROR; }
int fmi3ExitConfigurationMode() { return ERROR; }
int fmi3GetAdjointDerivative() { return ERROR; }
int fmi3GetBinary() { return ERROR; }
int fmi3GetBoolean() { return ERROR; }
int fmi3GetClock() { return ERROR; }
int fmi3GetContinuousStateDerivatives() { return ERROR; }
int fmi3GetContinuousStates() { return ERROR; }
int fmi3GetDirectionalDerivative() { return ERROR; }
int fmi3GetEventIndicators() { return ERROR; }
int fmi3GetIntervalDecimal() { return ERROR; }
int fmi3GetIntervalFraction() { return ERROR; }
int fmi3GetNominalsOfContinuousStates() { return ERROR; }
int fmi3GetNumberOfContinuousStates() { return ERROR; }
int fmi3GetNumberOfEventIndicators() { return ERROR; }
int fmi3GetNumberOfVariableDependencies() { return ERROR; }
int fmi3GetOutputDerivatives() { return ERROR; }
int fmi3GetShiftDecimal() { return ERROR; }
int fmi3GetShiftFraction() { return ERROR; }
int fmi3GetString() { return ERROR; }
int fmi3GetVariableDependencies() { return ERROR; }
int fmi3Reset() { return ERROR; }
int fmi3SetBinary() { return ERROR; }
int fmi3SetBoolean() { return ERROR; }
int fmi3SetClock() { return ERROR; }
int fmi3SetContinuousStates() { return ERROR; }
int fmi3SetIntervalDecimal() { return ERROR; }
int fmi3SetIntervalFraction() { return ERROR; }
int fmi3SetShiftDecimal() { return ERROR; }
int fmi3SetShiftFraction() { return ERROR; }
int fmi3SetString() { return ERROR; }
int fmi3SetTime() { return ERROR; }
int fmi3UpdateDiscreteStates() { return ERROR; }
int fmi3GetFloat32() { return ERROR; }
int fmi3GetInt8() { return ERROR; }
int fmi3GetUInt8() { return ERROR; }
int fmi3GetInt16() { return ERROR; }
int fmi3GetUInt16() { return ERROR; }
int fmi3GetInt32() { return ERROR; }
int fmi3GetUInt32() { return ERROR; }
int fmi3GetInt64() { return ERROR; }
int fmi3GetUInt64() { return ERROR; }
int fmi3SetFloat32() { return ERROR; }
int fmi3SetInt8() { return ERROR; }
int fmi3SetUInt8() { return ERROR; }
int fmi3SetInt16() { return ERROR; }
int fmi3SetUInt16() { return ERROR; }
int fmi3SetInt32() { return ERROR; }
int fmi3SetUInt32() { return ERROR; }
int fmi3SetInt64() { return ERROR; }
int fmi3SetUInt64() { return ERROR; }
int fmi3SetDebugLogging() { return OK; }
void *fmi3InstantiateModelExchange() { return NULL; }
void *fmi3InstantiateScheduledExecution() { return NULL; }
#endif
