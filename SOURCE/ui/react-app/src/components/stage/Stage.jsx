import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react';
import PropTypes from 'prop-types';
import StageModePlaceholder from './StageModePlaceholder';
import styles from './Stage.module.css';

function getRoundedRect(element) {
  const rect = element.getBoundingClientRect();
  return {
    x: Math.round(rect.x),
    y: Math.round(rect.y),
    width: Math.round(rect.width),
    height: Math.round(rect.height),
  };
}

function getModeContent(renderer, props) {
  if (!renderer) return null;
  if (typeof renderer === 'function') {
    return renderer(props || {});
  }
  return renderer;
}

const Stage = forwardRef(({
  mode = 'music',
  modeProps = {},
  modeRenderers = {},
  overlays = null,
  onStageRectChange,
}, forwardedRef) => {
  const stageRef = useRef(null);
  const lastRectKeyRef = useRef('');

  useImperativeHandle(forwardedRef, () => ({
    get element() {
      return stageRef.current;
    },
    getBoundingClientRect() {
      return stageRef.current?.getBoundingClientRect();
    },
  }), []);

  useEffect(() => {
    const node = stageRef.current;
    if (!node || !onStageRectChange || typeof window === 'undefined') return undefined;

    let animationFrameId = null;
    const publish = ({ force = false } = {}) => {
      if (animationFrameId !== null) {
        window.cancelAnimationFrame(animationFrameId);
      }
      animationFrameId = window.requestAnimationFrame(() => {
        if (!stageRef.current) return;
        const nextRect = getRoundedRect(stageRef.current);
        const nextKey = `${nextRect.x}:${nextRect.y}:${nextRect.width}:${nextRect.height}`;
        if (!force && nextKey === lastRectKeyRef.current) return;
        lastRectKeyRef.current = nextKey;
        onStageRectChange(nextRect);
      });
    };

    let observer = null;
    if (typeof window.ResizeObserver === 'function') {
      observer = new window.ResizeObserver(publish);
      observer.observe(node);
    }

    const publishCurrentRect = () => publish();
    const publishCurrentRectForBridge = () => publish({ force: true });

    window.addEventListener('resize', publishCurrentRect);
    window.addEventListener('viola-bridge-ready', publishCurrentRectForBridge);
    publish();

    return () => {
      if (animationFrameId !== null) {
        window.cancelAnimationFrame(animationFrameId);
      }
      if (observer) {
        observer.disconnect();
      }
      window.removeEventListener('resize', publishCurrentRect);
      window.removeEventListener('viola-bridge-ready', publishCurrentRectForBridge);
    };
  }, [mode, onStageRectChange]);

  const rendererEntries = Object.entries(modeRenderers);
  const hasActiveRenderer = Object.prototype.hasOwnProperty.call(modeRenderers, mode);

  return (
    <section
      ref={stageRef}
      className={styles.stage}
      data-testid="viola-stage"
      data-stage-mode={mode}
      aria-label="Viola stage"
    >
      {rendererEntries.map(([modeKey, renderer]) => {
        const active = modeKey === mode;
        return (
          <div
            key={modeKey}
            className={`${styles.modePanel} ${active ? styles.activePanel : styles.inactivePanel}`}
            data-testid={`stage-mode-panel-${modeKey}`}
            data-stage-mode-panel={modeKey}
            aria-hidden={!active}
          >
            {getModeContent(renderer, modeProps[modeKey])}
          </div>
        );
      })}
      {!hasActiveRenderer && (
        <div
          className={`${styles.modePanel} ${styles.activePanel}`}
          data-testid={`stage-mode-panel-${mode}`}
          data-stage-mode-panel={mode}
        >
          <StageModePlaceholder
            label={mode}
            title="Mode not yet built"
            detail="This Stage mode is registered in navigation but has no renderer yet."
          />
        </div>
      )}
      {overlays}
    </section>
  );
});

Stage.displayName = 'Stage';

Stage.propTypes = {
  mode: PropTypes.string,
  modeProps: PropTypes.object,
  modeRenderers: PropTypes.objectOf(PropTypes.oneOfType([
    PropTypes.node,
    PropTypes.func,
  ])).isRequired,
  overlays: PropTypes.node,
  onStageRectChange: PropTypes.func,
};

export default Stage;
