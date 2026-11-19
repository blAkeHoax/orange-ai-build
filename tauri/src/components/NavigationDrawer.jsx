import React, { useEffect } from 'react';
import {
  Home,
  MessageSquare,
  Zap,
  Brain,
  Activity,
  Shield,
  Wrench,
  Settings,
  X,
  Sun,
  Moon,
  Radio
} from 'lucide-react';
import { useTheme } from '../context/ThemeContext';
import orbImg from '../assets/logo_orb_64.png';
import '../styles/NavigationDrawer.css';

const NAV_ITEMS = [
  { id: 'home', label: 'Home', icon: Home, description: 'Workspace overview' },
  { id: 'chat', label: 'Chat', icon: MessageSquare, description: 'Direct AI conversation' },
  { id: 'jobs', label: 'Agent Jobs', icon: Zap, description: 'Autonomous scheduled jobs' },
  { id: 'memory', label: 'Memory', icon: Brain, description: 'Persistent contextual recall' },
  { id: 'activity', label: 'Activity', icon: Activity, description: 'System telemetry & logs' },
  { id: 'permissions', label: 'Permissions', icon: Shield, description: 'Execution control policies' },
  { id: 'tools', label: 'Tools', icon: Wrench, description: 'Capabilities & integrations' },
  { id: 'settings', label: 'Settings', icon: Settings, description: 'Preferences & environment' },
];

export default function NavigationDrawer({
  isOpen,
  onClose,
  activeView,
  onSelectView,
  runtimeStatus = 'online',
  runtimePid
}) {
  const { theme, toggleTheme } = useTheme();

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === 'Escape' && isOpen) {
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  const handleItemClick = (id) => {
    onSelectView(id);
    onClose();
  };

  return (
    <>
      {/* Backdrop overlay */}
      <div
        className={`drawer-backdrop ${isOpen ? 'is-open' : ''}`}
        onClick={onClose}
        aria-hidden={!isOpen}
      />

      {/* Drawer Container */}
      <aside
        className={`navigation-drawer ${isOpen ? 'is-open' : ''}`}
        aria-label="Application navigation"
      >
        {/* Header */}
        <div className="drawer-header">
          <div className="drawer-brand">
            <img src={orbImg} alt="" className="drawer-orb-icon" />
            <div className="drawer-brand-text">
              <span className="drawer-title">Orange AI</span>
              <span className="drawer-subtitle">DESKTOP 2.0</span>
            </div>
          </div>
          <button
            className="drawer-close-btn"
            onClick={onClose}
            title="Close menu (Esc)"
            type="button"
          >
            <X size={18} />
          </button>
        </div>

        {/* Navigation List */}
        <nav className="drawer-nav">
          <ul className="drawer-nav-list">
            {NAV_ITEMS.map((item) => {
              const Icon = item.icon;
              const isActive = activeView === item.id;
              return (
                <li key={item.id} className="drawer-nav-li">
                  <button
                    className={`drawer-nav-item ${isActive ? 'is-active' : ''}`}
                    onClick={() => handleItemClick(item.id)}
                    type="button"
                  >
                    <span className="drawer-nav-icon-wrapper">
                      <Icon size={18} />
                    </span>
                    <span className="drawer-nav-label-group">
                      <span className="drawer-nav-label">{item.label}</span>
                      <span className="drawer-nav-desc">{item.description}</span>
                    </span>
                    {isActive && <span className="drawer-active-indicator" />}
                  </button>
                </li>
              );
            })}
          </ul>
        </nav>

        {/* Footer */}
        <div className="drawer-footer">
          <button
            className="drawer-theme-toggle"
            onClick={toggleTheme}
            type="button"
          >
            <span className="theme-toggle-icon">
              {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
            </span>
            <span className="theme-toggle-label">
              {theme === 'dark' ? 'Switch to Light' : 'Switch to Dark'}
            </span>
          </button>

          <div className="drawer-status-pill">
            <span className="status-dot-pulse" />
            <span className="status-label">
              Python Engine: {runtimeStatus}
              {runtimePid ? ` (PID ${runtimePid})` : ''}
            </span>
          </div>
        </div>
      </aside>
    </>
  );
}
