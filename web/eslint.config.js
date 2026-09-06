import eslint from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/", "src/api/schema.d.ts", "src/api/schema.checked.d.ts"] },
  { files: ["e2e/**"], languageOptions: { globals: { process: "readonly", console: "readonly", fetch: "readonly", setTimeout: "readonly", document: "readonly" } } },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  {
    rules: {
      "no-restricted-globals": [
        "error",
        { name: "localStorage", message: "Tokens must never be persisted." },
        { name: "sessionStorage", message: "Use state/pending.ts helpers (non-secret data only)." },
      ],
    },
  },
);
