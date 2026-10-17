const STRIPE_HOSTS = new Set(["checkout.stripe.com", "billing.stripe.com"]);

/** True only for Stripe Checkout and Customer Portal HTTPS URLs. */
export function isAllowedStripeUrl(url) {
  if (!url || typeof url !== "string") return false;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  return parsed.protocol === "https:" && STRIPE_HOSTS.has(parsed.hostname);
}
