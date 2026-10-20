import assert from "node:assert/strict";
import { isAllowedStripeUrl } from "../src/stripeUrl.js";

assert.equal(isAllowedStripeUrl("https://checkout.stripe.com/c/pay/cs_test_x"), true);
assert.equal(isAllowedStripeUrl("https://billing.stripe.com/p/session/abc"), true);
assert.equal(isAllowedStripeUrl("https://evil.example/checkout.stripe.com"), false);
assert.equal(isAllowedStripeUrl("http://checkout.stripe.com/c/pay/cs_test_x"), false);
assert.equal(isAllowedStripeUrl("https://checkout.stripe.com.evil.example/pay"), false);
assert.equal(isAllowedStripeUrl("javascript:alert(1)"), false);
console.log("stripe url allowlist ok");
