// References, not installed or certified connectors. Only catalog IDs enter routes.
//
// `tint` is the vendor's brand colour, used for the monogram tile on each card.
// Brand SVGs exist for only two of these six, so a consistent monogram reads
// better than a mixed set of real logos and fallbacks.
export const API_REFERENCES = [
  { id: 'stripe', name: 'Stripe', category: 'Payments', description: 'Payments, subscriptions and billing.', source: 'https://github.com/stripe/openapi', tint: '#635BFF' },
  { id: 'github', name: 'GitHub', category: 'Developer', description: 'Repositories, issues, pull requests and releases.', source: 'https://github.com/github/rest-api-description', tint: '#24292F' },
  { id: 'slack', name: 'Slack', category: 'Communication', description: 'Channels, users, files and messages.', source: 'https://github.com/slackapi/slack-api-specs', tint: '#4A154B' },
  { id: 'twilio', name: 'Twilio', category: 'Telecom', description: 'Messaging, voice and phone numbers.', source: 'https://github.com/twilio/twilio-oai', tint: '#F22F46' },
  { id: 'plaid', name: 'Plaid', category: 'Financial data', description: 'Accounts, transactions and identity.', source: 'https://github.com/plaid/plaid-openapi', tint: '#0A85EA' },
  { id: 'digitalocean', name: 'DigitalOcean', category: 'Infrastructure', description: 'Droplets, Kubernetes, databases and networking.', source: 'https://github.com/digitalocean/openapi', tint: '#0080FF' },
] as const;

export function galleryReference(hash: string) {
  const id = new URLSearchParams(hash.split('?')[1] || '').get('reference');
  return API_REFERENCES.find(entry => entry.id === id);
}
