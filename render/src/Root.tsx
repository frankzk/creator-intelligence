import React from 'react';
import {Composition} from 'remotion';
import {ModuleVideo, defaultModuleProps} from './ModuleVideo';

const FPS = 30;

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="Module"
      component={ModuleVideo}
      width={1080}
      height={1920}
      fps={FPS}
      durationInFrames={150}
      defaultProps={defaultModuleProps}
      calculateMetadata={({props}) => ({
        durationInFrames: Math.max(1, Math.round(props.durationInSeconds * FPS)),
      })}
    />
  );
};
