import React, { useState, useEffect } from 'react';
import { ThemeProvider } from './context/ThemeContext';
import ApplicationShell from './components/ApplicationShell';
import RecoveryView from './components/RecoveryView';
import { getHandshakeStatus, retryHandshake } from './services/tauriBridge';
import './styles/designTokens.css';
import './styles/RecoveryView.css';

export default function App() {
  const [handshakeStatus, setHandshakeStatus] = useState(null); // null = checking, 'Ready' = ok, else = degraded
  const [diagnostic, setDiagnostic] = useState(null);
  const [isInitializing, setIsInitializing] = useState(true);

  const checkStatus = async () => {
    try {
      const res = await getHandshakeStatus();
      if (res && res.status === 'Ready') {
        setHandshakeStatus('Ready');
        setDiagnostic(res);
      } else {
        setHandshakeStatus(res?.status || 'StartupFailed');
        setDiagnostic(res || { status: 'StartupFailed', details: { error: 'Unknown startup failure' } });
      }
    } catch (err) {
      setHandshakeStatus('StartupFailed');
      setDiagnostic({ status: 'StartupFailed', details: { error: String(err) } });
    } finally {
      setIsInitializing(false);
    }
  };

  const handleRetry = async () => {
    try {
      const res = await retryHandshake();
      if (res && res.status === 'Ready') {
        setHandshakeStatus('Ready');
        setDiagnostic(res);
      } else {
        setHandshakeStatus(res?.status || 'StartupFailed');
        setDiagnostic(res || { status: 'StartupFailed', details: { error: 'Retry failed' } });
      }
    } catch (err) {
      setHandshakeStatus('StartupFailed');
      setDiagnostic({ status: 'StartupFailed', details: { error: String(err) } });
    }
  };

  useEffect(() => {
    checkStatus();
  }, []);

  if (isInitializing) {
    return (
      <ThemeProvider>
        <div className="startup-splash">
          <div className="startup-spinner" />
          <span className="startup-text">Initializing Orange AI Engine...</span>
        </div>
      </ThemeProvider>
    );
  }

  return (
    <ThemeProvider>
      {handshakeStatus === 'Ready' ? (
        <ApplicationShell />
      ) : (
        <RecoveryView diagnostic={diagnostic} onRetry={handleRetry} />
      )}
    </ThemeProvider>
  );
}
