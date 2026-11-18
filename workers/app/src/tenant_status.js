// A hosted tenant's status in the operator's registry (tenant_registry.js). Only an `active`
// tenant is scheduled by the tick or started by a run route; any other keeps its rows and its
// database, and gets no run. A module of its own with no workerd-only import, so the hosted
// router, which vitest runs under Node, can read it.
export const ACTIVE = "active";
export const TENANT_STATUSES = [ACTIVE, "paused"];
