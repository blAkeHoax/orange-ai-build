import React from 'react';

export default function StatusCard({ label, value, subtext, actionBtn }) {
  return (
    <div className="card">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span className="card-label">{label}</span>
        {actionBtn}
      </div>
      <div className="card-value">{value}</div>
      {subtext && <div className="card-subtext">{subtext}</div>}
    </div>
  );
}
