import { test } from "node:test";
import assert from "node:assert/strict";
import { isLocalRequest, validMode, csvCell } from "../lib/security.mjs";

const req = (headers) => ({ headers: new Headers(headers) });
test("local same-origin monitoring is allowed", () => {
  assert.equal(isLocalRequest(req({host:"127.0.0.1:3000",origin:"http://127.0.0.1:3000","sec-fetch-site":"same-origin"})), true);
  assert.equal(isLocalRequest(req({host:"localhost:3000"})), true);
});
test("remote origins, rebinding hosts and cross-site requests are rejected", () => {
  for (const headers of [{host:"attacker.test:3000"},{host:"localhost:3000",origin:"https://attacker.test"},{host:"localhost:3000","sec-fetch-site":"cross-site"},{host:"localhost.evil.test"},{}])
    assert.equal(isLocalRequest(req(headers)), false);
});
test("mode cannot become command arguments", () => {
  for (const mode of ["--live", "live & echo", "../.env", "LIVE", ""]) assert.equal(validMode(mode), false);
  assert.equal(validMode("paper"), true);
});
test("CSV escapes formulas and quotes", () => {
  assert.equal(csvCell('=HYPERLINK("bad")'), '"\'=HYPERLINK(""bad"")"');
  assert.equal(csvCell('normal,"title"'), '"normal,""title"""');
});
