import React from 'react';
import WelcomeSection from './WelcomeSection';
import OrbArtwork from './OrbArtwork';
import Composer from './Composer';
import BrandTrigger from './BrandTrigger';
import '../styles/HomeView.css';

export default function HomeView({ onToggleDrawer, isDrawerOpen, onSendMessage, userName = 'Sayan' }) {
  return (
    <div className="home-view-container">
      {/* Top-Left Brand Logo & Wordmark (Drawer Trigger) */}
      <div className="home-top-bar">
        <BrandTrigger onToggleDrawer={onToggleDrawer} isDrawerOpen={isDrawerOpen} />
      </div>

      {/* Main Hero Area */}
      <div className="home-hero-area">
        <WelcomeSection userName={userName} />
        <OrbArtwork />
      </div>

      {/* Floating Bottom Composer */}
      <Composer onSendMessage={onSendMessage} />
    </div>
  );
}
