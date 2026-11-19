import React, { useState, useEffect } from 'react';
import { Minus, Square, X, Sun, Moon, Download } from 'lucide-react';
import { useTheme } from '../context/ThemeContext';
import { minimizeWindow, toggleMaximizeWindow, closeWindow } from '../services/tauriBridge';
import { subscribeToUpdater, openUpdateModal } from '../services/updaterService';
import '../styles/TitleBar.css';

export default function TitleBar({ onToggleTheme }) {
  const { theme, toggleTheme } = useTheme();
  const [updaterState, setUpdaterState] = useState(null);

  useEffect(() => {
    const unsub = subscribeToUpdater(setUpdaterState);
    return () => unsub();
  }, []);

  const handleMinimize = (e) => {
    e.stopPropagation();
    minimizeWindow();
  };

  const handleMaximize = (e) => {
    e.stopPropagation();
    toggleMaximizeWindow();
  };

  const handleClose = (e) => {
    e.stopPropagation();
    closeWindow();
  };

  const handleThemeToggle = (e) => {
    e.stopPropagation();
    if (onToggleTheme) {
      onToggleTheme();
    } else {
      toggleTheme();
    }
  };

  return (
    <div className="titlebar" data-tauri-drag-region onDoubleClick={handleMaximize}>
      <div className="titlebar-left" data-tauri-drag-region>
        <span className="titlebar-app-name">Orange AI</span>
      </div>

      <div className="titlebar-center" data-tauri-drag-region></div>

      <div className="titlebar-right">
        {updaterState?.hasUpdateAvailable && (
          <button
            className="titlebar-update-btn"
            onClick={openUpdateModal}
            title={updaterState?.isDownloaded ? 'Update ready to install' : `Update available (v${updaterState.targetVersion})`}
            type="button"
          >
            <Download size={11} />
            <span>Update</span>
          </button>
        )}

        <button
          className="titlebar-theme-btn"
          onClick={handleThemeToggle}
          title={`Switch to ${theme === 'dark' ? 'Light' : 'Dark'} Theme`}
          type="button"
        >
          {theme === 'dark' ? <Sun size={14} /> : <Moon size={14} />}
        </button>

        <button
          className="titlebar-control-btn btn-minimize"
          onClick={handleMinimize}
          title="Minimize"
          type="button"
        >
          <Minus size={13} />
        </button>

        <button
          className="titlebar-control-btn btn-maximize"
          onClick={handleMaximize}
          title="Maximize"
          type="button"
        >
          <Square size={11} />
        </button>

        <button
          className="titlebar-control-btn btn-close"
          onClick={handleClose}
          title="Close"
          type="button"
        >
          <X size={13} />
        </button>
      </div>
    </div>
  );
}
