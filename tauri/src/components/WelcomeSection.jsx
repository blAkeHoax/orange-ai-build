import React from 'react';
import '../styles/WelcomeSection.css';

export default function WelcomeSection({ userName = 'Sayan' }) {
  return (
    <div className="welcome-section">
      <h1 className="welcome-title">
        Welcome <span className="welcome-name-highlight">{userName}</span>,
        <br />
        how can I help today?
      </h1>
    </div>
  );
}
