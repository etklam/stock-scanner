import { createContext, useContext, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiClient } from "./api/client";

// The local API token lives ONLY in this ref: never localStorage,
// sessionStorage, URL, or compiled output. A reload simply asks again.

type AuthState = {
  client: ApiClient | null;
  connected: boolean;
  connect: (token: string) => Promise<void>;
  disconnect: () => void;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children, onUnauthorized }: { children: ReactNode; onUnauthorized: () => void }) {
  const tokenRef = useRef<string | null>(null);
  const [nonce, setNonce] = useState(0);
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
        setNonce((n) => n + 1);
        onUnauthorized();
      },
    );

  const connect = async (token: string) => {
    tokenRef.current = token;
    clientRef.current = build();
    // Verify before claiming connected: a bad token never enters the app.
    await clientRef.current.watchlists();
    setNonce((n) => n + 1);
  };

  const disconnect = () => {
    tokenRef.current = null;
    clientRef.current = null;
    setNonce((n) => n + 1);
  };

  return (
    <AuthContext.Provider
      value={{ client: clientRef.current, connected: tokenRef.current !== null, connect, disconnect }}
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
