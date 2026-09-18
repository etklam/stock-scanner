import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "./auth";

afterEach(() => vi.unstubAllGlobals());

function Status() {
  const auth = useAuth();
  return <span>{auth.checking ? "checking" : auth.connected ? "connected" : "disconnected"}</span>;
}

it("automatically establishes the browser session on mount", async () => {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify({ csrf_token: "memory-only" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);

  render(
    <AuthProvider onUnauthorized={vi.fn()}>
      <Status />
    </AuthProvider>,
  );

  expect(screen.getByText("checking")).toBeTruthy();
  await waitFor(() => expect(screen.getByText("connected")).toBeTruthy());
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/auth/session",
    expect.objectContaining({ credentials: "same-origin" }),
  );
});
