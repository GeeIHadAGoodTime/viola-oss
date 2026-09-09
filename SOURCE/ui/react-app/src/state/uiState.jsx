import React, { createContext, useContext, useMemo, useReducer } from 'react';
import PropTypes from 'prop-types';

export const UI_ACTIONS = Object.freeze({
  SET_COMMAND_PALETTE_OPEN: 'ui/commandPalette/setOpen',
});

export const initialUiState = Object.freeze({
  commandPaletteOpen: false,
});

export function uiStateReducer(state, action) {
  switch (action.type) {
    case UI_ACTIONS.SET_COMMAND_PALETTE_OPEN: {
      const commandPaletteOpen = Boolean(action.open);
      if (state.commandPaletteOpen === commandPaletteOpen) return state;
      return { ...state, commandPaletteOpen };
    }
    default:
      return state;
  }
}

const UiStateContext = createContext(null);
const UiStateDispatchContext = createContext(null);

export function setCommandPaletteOpen(open) {
  return {
    type: UI_ACTIONS.SET_COMMAND_PALETTE_OPEN,
    open,
  };
}

export function UiStateProvider({ children, initialState = null }) {
  const [state, dispatch] = useReducer(
    uiStateReducer,
    initialState ? { ...initialUiState, ...initialState } : initialUiState,
  );
  const dispatchValue = useMemo(() => dispatch, [dispatch]);

  return (
    <UiStateDispatchContext.Provider value={dispatchValue}>
      <UiStateContext.Provider value={state}>
        {children}
      </UiStateContext.Provider>
    </UiStateDispatchContext.Provider>
  );
}

UiStateProvider.propTypes = {
  children: PropTypes.node.isRequired,
  initialState: PropTypes.shape({
    commandPaletteOpen: PropTypes.bool,
  }),
};

export function useUiSelector(selector = (state) => state) {
  const state = useContext(UiStateContext);
  if (!state) {
    throw new Error('useUiSelector must be used inside UiStateProvider');
  }
  return selector(state);
}

export function useUiStateDispatch() {
  const dispatch = useContext(UiStateDispatchContext);
  if (!dispatch) {
    throw new Error('useUiStateDispatch must be used inside UiStateProvider');
  }
  return dispatch;
}
