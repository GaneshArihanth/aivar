/* Fetch wrapper.
 *
 * The backend speaks one error envelope — {"error": {type, message, ...}} —
 * wrapped by FastAPI in {"detail": …} for raised HTTPExceptions and returned
 * bare for JSONResponse. Both shapes are normalised here so no view has to
 * know which layer refused it.
 */

export class ApiError extends Error {
  constructor(message, { status, type, field, detail, body } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.type = type;
    this.field = field;
    this.detail = detail;
    this.body = body;
  }
}

function normalise(body, status) {
  const envelope = body?.detail?.error || body?.error;
  if (envelope) {
    return new ApiError(envelope.message || "Request failed", {
      status,
      type: envelope.type,
      field: envelope.field,
      detail: envelope,
      body,
    });
  }
  // FastAPI validation errors arrive as a list under detail.
  if (Array.isArray(body?.detail)) {
    const first = body.detail[0];
    return new ApiError(first?.msg || "Validation failed", {
      status,
      type: "validation_error",
      field: first?.loc?.[first.loc.length - 1],
      detail: body.detail,
      body,
    });
  }
  return new ApiError(
    typeof body?.detail === "string" ? body.detail : `Request failed (${status})`,
    { status, body }
  );
}

async function request(method, path, payload) {
  const options = { method, headers: {} };
  if (payload !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  }

  let response;
  try {
    response = await fetch(path, options);
  } catch (cause) {
    throw new ApiError("Cannot reach the server.", { status: 0, type: "network" });
  }

  if (response.status === 204) return null;

  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw normalise(body, response.status);
  return body;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, payload) => request("POST", path, payload ?? {}),
  patch: (path, payload) => request("PATCH", path, payload),
  put: (path, payload) => request("PUT", path, payload),
  del: (path) => request("DELETE", path),
};
