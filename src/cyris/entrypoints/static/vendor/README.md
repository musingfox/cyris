# Vendored browser code

## `messageformat.js`

The JS formatter of the UI catalogs (ADR-0027): the `messageformat` package, version **4.1.0**
(npm, integrity `sha512-ZLId0BYfsNhC6/MRwVFw1yTLgG9cbJwfTsjYSS2C59PdfJWVRsX7hthADvsXefBtTg2FKso0VAJmMJJOtnJ75w==`),
bundled into one ES module that exports only `MessageFormat`. It has no dependencies of its own.

Licence: Apache-2.0, copyright the messageformat contributors; the full text is
`messageformat.LICENSE` beside it. The package ships no NOTICE file.

Built with bun 1.4.4 in an empty directory outside the repo:

```sh
bun init -y
bun add messageformat@4.1.0
printf 'export { MessageFormat } from "messageformat";\n' > entry.js
bun build entry.js --minify --target=browser --outfile messageformat.js
cp node_modules/messageformat/LICENSE messageformat.LICENSE
```

The output is 23,035 bytes (7,645 gzipped at gzip's default level, 7,644 at `-9`), SHA-256
`ad39eb7bc9291b17d08e7d692a6f7c219f4521c3a6d4ba80efdccb0a167cbd1c`. Replace the file only by
rerunning these steps at a new pinned version, and update the version, hash and sizes here.
