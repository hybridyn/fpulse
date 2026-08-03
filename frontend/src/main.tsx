import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { WORKSPACES_ENABLED } from './config/edition';
import './styles/globals.css';

// Dark mode removed — force light mode and clear any persisted dark preference.
document.documentElement.classList.remove('dark');
try { localStorage.removeItem('fpulse_theme'); } catch {}

// OSS is single-operator: pin to the shared `default` workspace before any
// component or API call reads localStorage. A previous build's switcher may
// have parked a Personal workspace id here, which would filter the operator's
// pipelines out of view ("vanished pipelines"). Clearing it heals the current
// session without a re-login. (Multi-workspace is a Plus capability.)
if (!WORKSPACES_ENABLED) {
  try {
    localStorage.setItem('fpulse_workspace_id', 'default');
    localStorage.removeItem('fpulse_workspaces');
  } catch {}
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
