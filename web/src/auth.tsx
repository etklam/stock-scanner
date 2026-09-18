import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiClient } from "./api/client";

// Browser auth uses a strict cookie plus an in-memory CSRF token. The optional
// advanced Bearer token also lives only in this ref, never storage or the URL.

type AuthState = {
  client: ApiClient | null;
  connected: boolean;
  checking: boolean;
  retrySession: () => Promise<void>;
  connect: (token: string) => Promise<void>;
  disconnect: () => void;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children, onUnauthorized }: { children: ReactNode; onUnauthorized: () => void }) {
  const tokenRef = useRef<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const [checking, setChecking] = useState(true);
  const clientRef = useRef<ApiClient | null>(null);

  const build = () =>
    new ApiClient(
      "",
      () => tokenRef.current,
      () => {
        // 401 / token rotation: drop the token and sensitive caches; the
        // non-secret pending intent survives in sessionStorage.
        tokenRef.current = null;
        clientRef.current = null;
        setChecking(false);
        setNonce((n) => n + 1);
        onUnauthorized();
      },
    );

  const retrySession = async () => {
    setChecking(true);
    const client = build();
    try {
      await client.establishBrowserSession();
      clientRef.current = client;
    } finally {
      setChecking(false);
      setNonce((n) => n + 1);
    }
  };

  useEffect(() => {
    void retrySession().catch(() => undefined);
  }, []);

  const connect = async (token: string) => {
    tokenRef.current = token;
    setChecking(true);
    const client = build();
    // Verify before claiming connected: a bad token never enters the app.
    try {
      await client.watchlists();
      clientRef.current = client;
      setNonce((n) => n + 1);
    } catch (cause) {
      tokenRef.current = null;
      clientRef.current = null;
      setNonce((n) => n + 1);
      throw cause;
    } finally {
      setChecking(false);
    }
  };

  const disconnect = () => {
    tokenRef.current = null;
    clientRef.current = null;
    setNonce((n) => n + 1);
  };

  return (
    <AuthContext.Provider
      value={{
        client: clientRef.current,
        connected: clientRef.current !== null,
        checking,
        retrySession,
        connect,
        disconnect,
      }}
      key={nonce}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const value = useContext(AuthContext);
  if (!value) throw new Error("AuthProvider missing");
  return value;
}
