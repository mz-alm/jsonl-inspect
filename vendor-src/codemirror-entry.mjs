// Entry point for the bundled CodeMirror distribution.
// Imports the pieces we use and exposes them as named exports. Bundled with
// esbuild into static/vendor/codemirror.js; that file is what the inspector
// loads at runtime. Regenerate by running:
//   npx esbuild --bundle vendor-src/codemirror-entry.mjs \
//       --format=esm --outfile=static/vendor/codemirror.js --minify

// Don't import @codemirror/state directly — it's a transitive dep of every
// other CM package. Installing/importing it independently can produce two
// copies in the bundle (the dreaded "multiple instances of @codemirror/state"
// runtime error). Let the deps resolve through their natural tree.
import { EditorView, basicSetup } from "codemirror";
import { json } from "@codemirror/lang-json";
import { oneDark } from "@codemirror/theme-one-dark";

export { EditorView, basicSetup, json, oneDark };
