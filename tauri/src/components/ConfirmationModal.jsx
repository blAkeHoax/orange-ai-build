import React from 'react';
import { AlertTriangle, ShieldCheck, XCircle } from 'lucide-react';
import '../styles/ConfirmationModal.css';

export default function ConfirmationModal({ confirmation, onConfirm }) {
  if (!confirmation) return null;

  return (
    <div className="confirmation-modal-backdrop">
      <div className="confirmation-modal-dialog">
        <div className="confirmation-dialog-header">
          <div className="confirmation-dialog-badge">
            <AlertTriangle size={18} />
          </div>
          <div className="confirmation-dialog-title">Action Authorization Required</div>
        </div>

        <div className="confirmation-dialog-body">
          <p className="confirmation-dialog-prompt">
            {confirmation.prompt_text || confirmation.prompt || 'An operation requires your explicit authorization.'}
          </p>
          <div className="confirmation-dialog-id">
            Request ID: <code>{confirmation.confirmation_id}</code>
          </div>
        </div>

        <div className="confirmation-dialog-actions">
          <button
            className="confirm-btn btn-deny"
            onClick={() => onConfirm(confirmation.confirmation_id, 'no')}
            type="button"
          >
            <XCircle size={15} />
            <span>Cancel / Deny</span>
          </button>
          <button
            className="confirm-btn btn-authorize"
            onClick={() => onConfirm(confirmation.confirmation_id, 'yes')}
            type="button"
          >
            <ShieldCheck size={15} />
            <span>Authorize & Execute</span>
          </button>
        </div>
      </div>
    </div>
  );
}
