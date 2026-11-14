/// voice_ui.dart — makes a list screen answer spoken navigation.
///
/// The hub says "scroll down" or "select <id>"; AppState._handleUi turns
/// the frame into a [VoiceUiCommand] on AppState.voiceUi. Wrap the screen's
/// list in a [VoiceUiScope]: it owns a ScrollController for the list, acts
/// only while its route is the one on top, scrolls on its own, and hands a
/// select to [onSelect] with the id the screen advertised in
/// AppState.uiContext. Screens stay stateless; this is the one stateful
/// thing they need.
library;

import 'package:flutter/material.dart';

import '../app_state.dart';

class VoiceUiScope extends StatefulWidget {
  const VoiceUiScope({
    super.key,
    required this.state,
    required this.builder,
    this.onSelect,
  });

  final AppState state;

  /// Builds the scrollable; give it the controller.
  final Widget Function(BuildContext context, ScrollController scroll) builder;

  /// A row named by id, or null for a screen with nothing to pick.
  final void Function(BuildContext context, String id, String label)? onSelect;

  @override
  State<VoiceUiScope> createState() => _VoiceUiScopeState();
}

class _VoiceUiScopeState extends State<VoiceUiScope> {
  final ScrollController _scroll = ScrollController();

  @override
  void initState() {
    super.initState();
    widget.state.voiceUi.addListener(_onVoiceUi);
  }

  @override
  void didUpdateWidget(VoiceUiScope old) {
    super.didUpdateWidget(old);
    if (!identical(old.state, widget.state)) {
      old.state.voiceUi.removeListener(_onVoiceUi);
      widget.state.voiceUi.addListener(_onVoiceUi);
    }
  }

  @override
  void dispose() {
    widget.state.voiceUi.removeListener(_onVoiceUi);
    _scroll.dispose();
    super.dispose();
  }

  void _onVoiceUi() {
    final cmd = widget.state.voiceUi.value;
    if (cmd == null || !mounted) return;
    // Only the screen on top answers. A screen further down the stack is
    // still mounted and still listening, and must stay still.
    if (!(ModalRoute.of(context)?.isCurrent ?? false)) return;
    switch (cmd.action) {
      case 'scroll':
        if (_scrollBy(cmd.direction)) cmd.handled = true;
        break;
      case 'select':
        final onSelect = widget.onSelect;
        if (onSelect != null) {
          cmd.handled = true;
          onSelect(context, cmd.id, cmd.label);
        }
        break;
    }
  }

  /// True when there was a list to move.
  bool _scrollBy(String direction) {
    if (!_scroll.hasClients) return false;
    final pos = _scroll.position;
    final page = pos.viewportDimension * 0.8;
    double target;
    switch (direction) {
      case 'down':
        target = pos.pixels + page;
        break;
      case 'up':
        target = pos.pixels - page;
        break;
      case 'top':
        target = pos.minScrollExtent;
        break;
      case 'bottom':
        target = pos.maxScrollExtent;
        break;
      default:
        return false;
    }
    target = target.clamp(pos.minScrollExtent, pos.maxScrollExtent);
    _scroll.animateTo(target,
        duration: const Duration(milliseconds: 320), curve: Curves.easeOut);
    return true;
  }

  @override
  Widget build(BuildContext context) => widget.builder(context, _scroll);
}