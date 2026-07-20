import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles/globals.css';

// Dark mode removed — force light mode and clear any persisted dark preference.
document.documentElement.classList.remove('dark');
try { localStorage.removeItem('fpulse_theme'); } catch {}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
