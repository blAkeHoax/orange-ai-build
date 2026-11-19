import React from 'react';
import orbMaster from '../assets/logo_orb_master.png';
import '../styles/OrbArtwork.css';

export default function OrbArtwork() {
  return (
    <div className="orb-artwork-wrapper" aria-hidden="true">
      {/* Ambient glowing radial halo */}
      <div className="orb-ambient-halo" />

      {/* 3D faceted orb */}
      <div className="orb-image-container">
        <img
          src={orbMaster}
          alt="Orange AI Polyhedral Orb"
          className="orb-faceted-image"
          draggable={false}
        />
      </div>
    </div>
  );
}
