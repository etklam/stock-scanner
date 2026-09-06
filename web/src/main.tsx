import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { AuthProvider } from "./auth";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");

function Mounted() {
  // The query cache holds run/result data behind the bearer token: a 401 or
  // token rotation clears it along with the in-memory credential.
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: false, refetchOnWindowFocus: false, staleTime: 5_000 },
        },
      }),
  );
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider onUnauthorized={() => queryClient.clear()}>
        <App queryClient={queryClient} />
      </AuthProvider>
    </QueryClientProvider>
  );
}

createRoot(root).render(<Mounted />);
