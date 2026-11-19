import React from 'react';
import orbImg from '../assets/logo_orb_64.png';
import '../styles/BrandTrigger.css';

export default function BrandTrigger({ onToggleDrawer, isDrawerOpen }) {
  return (
    <button
      className={`brand-trigger ${isDrawerOpen ? 'is-active' : ''}`}
      onClick={onToggleDrawer}
      title="Open navigation menu (or press Esc to close)"
      type="button"
      aria-expanded={isDrawerOpen}
    >
      <div className="brand-orb-container">
        <img src={orbImg} alt="Orange AI Orb" className="brand-orb-icon" />
      </div>
      <div className="brand-text-container">
        <div className="brand-wordmark">
          <span className="wordmark-orange">Orange</span>{' '}
          <span className="wordmark-ai">AI</span>
        </div>
        <div className="brand-tagline">FLUID COGNITION ARCHITECTURE</div>
      </div>
    </button>
  );
}
